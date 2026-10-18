"""Public OpenAI response contracts at the executor boundary."""
from __future__ import annotations

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
        return type(value) is int and value >= 0
    require(isinstance(payload, Mapping), 'The mapped response must be a JSON object.')
    if intent == 'chat':
        require(payload.get('object') == ('chat.completion.chunk' if stream else 'chat.completion'),
                'The mapped chat response has the wrong object type.')
        require(isinstance(payload.get('id'), str) and integer(payload.get('created')) and
                isinstance(payload.get('model'), str), 'The mapped chat response needs id, created and model fields.')
        choices = payload.get('choices')
        require(isinstance(choices, list), 'The mapped chat response needs a choices array.')
        for choice in choices:
            require(isinstance(choice, Mapping) and integer(choice.get('index')), 'Each chat choice needs an integer index.')
            message = choice.get('delta' if stream else 'message')
            require(isinstance(message, Mapping), 'Each chat choice needs a message or delta object.')
            require(stream or message.get('role') == 'assistant', 'A chat message must have the assistant role.')
            require(message.get('content') is None or isinstance(message.get('content'), str), 'Chat content must be text or null.')
            require('finish_reason' in choice and choice['finish_reason'] in
                    (None, 'stop', 'length', 'tool_calls', 'content_filter', 'function_call'), 'A chat choice has an invalid finish reason.')
    elif intent == 'embeddings':
        require(not stream and payload.get('object') == 'list' and isinstance(payload.get('model'), str),
                'The mapped embeddings response needs object list and model fields.')
        items = payload.get('data')
        require(isinstance(items, list), 'The mapped embeddings response needs a data array.')
        for item in items:
            require(isinstance(item, Mapping) and item.get('object') == 'embedding' and integer(item.get('index')),
                    'Each embedding needs object embedding and an integer index.')
            value = item.get('embedding')
            require(isinstance(value, str) or (isinstance(value, list) and all(type(v) in (int, float) and math.isfinite(v) for v in value)),
                    'Each embedding must be a numeric array or a base64 string.')
    if 'usage' in payload and payload['usage'] is not None:
        usage = payload['usage']
        fields = ('prompt_tokens','total_tokens') if intent == 'embeddings' else ('prompt_tokens','completion_tokens') if stream else ('prompt_tokens','completion_tokens','total_tokens')
        require(isinstance(usage, Mapping) and all(integer(usage.get(name)) for name in fields),
                'The mapped usage object has missing or invalid token counts.')
    return payload
