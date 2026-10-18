"""Public OpenAI response contracts at the executor boundary."""
from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping
from gateway.engine_protocol.errors import ProtocolError


def validate(payload, intent: str, *, stream: bool = False):
    def require(condition, reason):
        if not condition:
            raise ProtocolError(reason, code='malformed_response')
    if isinstance(payload, bytes):
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeError):
            raise ProtocolError('The mapped stream event must be JSON.', code='malformed_response') from None
    require(not stream or intent == 'chat', 'Only chat supports a public event stream.')
    require(intent != 'speech', 'Speech synthesis must return an audio asset.')
    def integer(value):
        # Counts and indexes stay within the range JSON readers hold exactly.
        return type(value) is int and 0 <= value <= 2 ** 53 - 1
    def finite_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False
    require(isinstance(payload, Mapping), 'The mapped response must be a JSON object.')
    if intent == 'chat':
        require(payload.get('object') == ('chat.completion.chunk' if stream else 'chat.completion'),
                'The mapped chat response has the wrong object type.')
        require(isinstance(payload.get('id'), str) and integer(payload.get('created')) and
                isinstance(payload.get('model'), str), 'The mapped chat response needs id, created and model fields.')
        choices = payload.get('choices')
        require(isinstance(choices, list) and (stream or choices),
                'The mapped chat response needs a choices array; a complete response has at least one choice.')
        for choice in choices:
            require(isinstance(choice, Mapping) and integer(choice.get('index')), 'Each chat choice needs an integer index.')
            message = choice.get('delta' if stream else 'message')
            require(isinstance(message, Mapping), 'Each chat choice needs a message or delta object.')
            require(stream or message.get('role') == 'assistant', 'A chat message must have the assistant role.')
            require(message.get('content') is None or isinstance(message.get('content'), str), 'Chat content must be text or null.')
            if message.get('tool_calls') is not None:
                calls = message['tool_calls']
                require(isinstance(calls, list), 'Chat tool calls must be an array.')
                for call in calls:
                    require(isinstance(call, Mapping), 'Each chat tool call must be an object.')
                    if stream:
                        require(integer(call.get('index')), 'Each streamed tool call needs an integer index.')
                        require('id' not in call or isinstance(call['id'], str), 'A streamed tool id must be text.')
                        require('type' not in call or call['type'] == 'function', 'A streamed tool type must be function.')
                        if 'function' not in call:
                            continue
                    else:
                        require(isinstance(call.get('id'), str) and call.get('type') == 'function',
                                'Each chat tool call needs an id and type function.')
                    function = call.get('function')
                    require(isinstance(function, Mapping), 'Each tool call needs a function object.')
                    require((stream and 'name' not in function) or isinstance(function.get('name'), str),
                            'A tool function name must be a string.')
                    require((stream and 'arguments' not in function) or isinstance(function.get('arguments'), str),
                            'Tool function arguments must be a JSON string.')
            if message.get('function_call') is not None:
                function = message['function_call']
                require(isinstance(function, Mapping) and
                        ((stream and 'name' not in function) or isinstance(function.get('name'), str)) and
                        ((stream and 'arguments' not in function) or isinstance(function.get('arguments'), str)),
                        'A function call needs string name and arguments fields.')
            # A streamed chunk may be unfinished (null). A complete response must say why it ended.
            require('finish_reason' in choice and (choice['finish_reason'] in
                    ('stop', 'length', 'tool_calls', 'content_filter', 'function_call') or
                    (stream and choice['finish_reason'] is None)), 'A chat choice has an invalid finish reason.')
    elif intent == 'embeddings':
        require(not stream and payload.get('object') == 'list' and isinstance(payload.get('model'), str),
                'The mapped embeddings response needs object list and model fields.')
        items = payload.get('data')
        require(isinstance(items, list), 'The mapped embeddings response needs a data array.')
        for item in items:
            require(isinstance(item, Mapping) and item.get('object') == 'embedding' and integer(item.get('index')),
                    'Each embedding needs object embedding and an integer index.')
            value = item.get('embedding')
            require(isinstance(value, str) or (isinstance(value, list) and all(finite_number(v) for v in value)),
                    'Each embedding must be a numeric array or a base64 string.')
            if isinstance(value, str):
                require(value != '', 'The encoded embedding must not be empty.')
                try:
                    decoded = base64.b64decode(value, validate=True)
                except (ValueError, binascii.Error):
                    require(False, 'The encoded embedding must be valid base64.')
                require(len(decoded) % 4 == 0, 'The encoded embedding must contain float32 values.')
    if 'usage' in payload and payload['usage'] is not None:
        usage = payload['usage']
        fields = ('prompt_tokens','total_tokens') if intent == 'embeddings' else ('prompt_tokens','completion_tokens') if stream else ('prompt_tokens','completion_tokens','total_tokens')
        require(isinstance(usage, Mapping) and all(integer(usage.get(name)) for name in fields),
                'The mapped usage object has missing or invalid token counts.')
    return payload
