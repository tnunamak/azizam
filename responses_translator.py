"""Translation layer between OpenAI Responses API and Chat Completions API.

The proxy's downstream backends (llama-server, TabbyAPI) speak Chat
Completions. We expose the newer Responses API by translating at the proxy
boundary.

Mapping summary (Responses -> Chat Completions, on request):
  input (str | list[item])      -> messages (list of role/content dicts)
  instructions (str)            -> prepended `system` message
  previous_response_id          -> walked through responses_store and the
                                   prior conversation is reconstructed as
                                   leading messages
  tools[type=function]          -> tools[type=function]  (shape differs)
  tools[type=custom]            -> function with one string `input` parameter
                                   (grammar is advisory description text)
  tools[type=namespace]         -> flattened functions with request-local aliases
  tools[type=tool_search]       -> function when execution=client; hosted search rejected
  tools[type=hosted]            -> 400 not_supported_error
  tool_choice                   -> tool_choice (compatible shapes pass through)
  parallel_tool_calls           -> parallel_tool_calls
  text.format.type=text         -> (drop)
  text.format.type=json_object  -> response_format={"type": "json_object"}
  text.format.type=json_schema  -> response_format={"type": "json_schema", ...}
  max_output_tokens             -> max_tokens
  max_completion_tokens         -> max_tokens (fallback spelling)
  temperature/top_p/metadata    -> pass through
  reasoning.effort=none|minimal -> append "/no_think" to system prompt
                                   + chat_template_kwargs.enable_thinking=false
  reasoning.effort=low|med|high -> no-op (local models have a binary switch)
  reasoning.effort=xhigh        -> no-op
  stream                        -> stream (event format differs; see translate_stream)

Mapping back (Chat Completions -> Responses, on response):
  choices[0].message.content    -> output: [ {type: message, role: assistant,
                                              content: [{type: output_text, text: ...}]} ]
  choices[0].message.tool_calls -> function_call, custom_tool_call, or client tool_search_call;
                                   original namespace/name restored by the request mapping
  usage                         -> usage (input_tokens / output_tokens / total_tokens)
  finish_reason=length          -> status=incomplete, incomplete_details

Notes:
- We do NOT implement server-side tool LOOP. If the model emits a tool call,
  the client receives it and is expected to chain via previous_response_id +
  function_call_output input items.
- Hosted tools (web_search, file_search, code_interpreter, computer_use,
  computer_use_preview, image_generation, mcp, file_search_preview,
  web_search_preview, web_search_2025_*, code_interpreter_preview, shell,
  apply_patch) are rejected.
"""

from __future__ import annotations

import codecs
import copy
import hashlib
import json
import secrets
import time
from typing import Any, Optional


HOSTED_TOOL_TYPES = {
    "web_search",
    "web_search_preview",
    "web_search_2025_06_24",
    "web_search_2025_08_25",
    "file_search",
    "file_search_preview",
    "code_interpreter",
    "code_interpreter_preview",
    "computer_use",
    "computer_use_preview",
    "image_generation",
    "mcp",
    "shell",
    "local_shell",
    "apply_patch",
}


class ResponsesTranslationError(Exception):
    """400-class error raised during translation."""

    def __init__(self, message: str, code: str = "invalid_request_error", param: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.param = param


def new_response_id() -> str:
    """OpenAI uses 'resp_' + hex. Mimic the prefix; randomness is fine."""
    return "resp_" + secrets.token_hex(24)


def _coerce_content_to_text(content: Any) -> str:
    """Flatten a Responses content array (list of parts) or a string into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                # Responses content part types: input_text, output_text,
                # input_image, input_file, refusal. This text flattener is
                # used for non-vision contexts, so non-text parts are skipped.
                t = c.get("type")
                if t in ("input_text", "output_text", "text"):
                    txt = c.get("text", "")
                    if isinstance(txt, str):
                        parts.append(txt)
                elif t == "refusal":
                    refusal = c.get("refusal", "")
                    if isinstance(refusal, str):
                        parts.append(refusal)
        return "".join(parts)
    if isinstance(content, dict):
        # Single-part dict.
        return _coerce_content_to_text([content])
    return str(content)


def _content_to_chat_content(content: Any, *, param: str = "input") -> Any:
    """Map Responses message content to Chat Completions content.

    Text-only content stays a plain string to preserve the existing request
    shape. Multimodal content becomes OpenAI chat content parts so the chat
    route can detect and forward vision inputs.
    """
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return _coerce_content_to_text(content)

    parts: list[dict] = []
    for index, c in enumerate(content):
        if isinstance(c, str):
            if c:
                parts.append({"type": "text", "text": c})
            continue
        if not isinstance(c, dict):
            text = str(c)
            if text:
                parts.append({"type": "text", "text": text})
            continue
        t = c.get("type")
        if t == "input_file" or (t in ("input_image", "image_url", "image") and c.get("file_id")):
            raise ResponsesTranslationError(
                "Azizam cannot resolve file attachments. Use an image URL or data URL for images.",
                code="unsupported_operation",
                param=f"{param}[{index}]",
            )
        if t in ("input_text", "output_text", "text"):
            text = c.get("text", "")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
            continue
        if t in ("input_image", "image_url", "image"):
            image_url = c.get("image_url") or c.get("url")
            detail = c.get("detail")
            if isinstance(image_url, dict):
                if not isinstance(detail, str):
                    detail = image_url.get("detail")
                image_url = image_url.get("url") or image_url.get("image_url")
            if isinstance(image_url, str) and image_url:
                image_part: dict = {"type": "image_url", "image_url": {"url": image_url}}
                if isinstance(detail, str) and detail:
                    image_part["image_url"]["detail"] = detail
                parts.append(image_part)
            continue
        if t == "refusal":
            refusal = c.get("refusal", "")
            if isinstance(refusal, str) and refusal:
                parts.append({"type": "text", "text": refusal})

    if not any(part.get("type") == "image_url" for part in parts):
        return "".join(part.get("text", "") for part in parts if part.get("type") == "text")
    return parts


def _input_item_to_messages(item: dict, *, param: str = "input") -> list[dict]:
    """Map one Responses input item to one or more Chat Completions messages.

    Responses input items include:
      - message (role + content)
      - function_call (assistant tool call)
      - function_call_output (tool result, role=tool)
      - custom_tool_call (freeform input wrapped as function arguments)
      - custom_tool_call_output (tool result, role=tool)
      - reasoning, refusal, image_generation_call, etc. -> dropped
    """
    if not isinstance(item, dict):
        return []
    itype = item.get("type")
    if itype == "additional_tools":
        raise ResponsesTranslationError(
            "Azizam does not support additional_tools input declarations. Put function and custom tools in tools.",
            code="unsupported_operation", param=f"{param}.type",
        )
    if itype == "custom_tool_call" and not isinstance(item.get("input"), str):
        raise ResponsesTranslationError("Custom tool call input must be a string.", param=f"{param}.input")
    if itype in ("tool_search_call", "tool_search_output"):
        if item.get("execution") != "client":
            raise ResponsesTranslationError(
                "Azizam supports client-executed tool_search only; hosted search is unavailable.",
                code="hosted_tool_not_supported", param=f"{param}.execution",
            )
        if itype == "tool_search_call":
            if not isinstance(item.get("arguments"), dict):
                raise ResponsesTranslationError("tool_search arguments must be an object.", param=f"{param}.arguments")
            return _input_item_to_messages({**item, "type": "function_call", "name": "tool_search",
                                           "arguments": json.dumps(item["arguments"], ensure_ascii=False)}, param=param)
        return [{"role": "tool", "tool_call_id": item.get("call_id") or "",
                 "content": json.dumps({"tools": item.get("tools", [])}, ensure_ascii=False),
                 "_azizam_discovered_tools": item.get("tools", [])}]
    # "message" is the common case. Easy-input messages without an explicit
    # type still have role + content and should be treated as messages.
    if itype == "message" or (itype is None and "role" in item):
        role = item.get("role") or "user"
        # Chat Completions uses "system" not "developer". Map developer->system.
        if role == "developer":
            role = "system"
        return [{"role": role, "content": _content_to_chat_content(item.get("content"), param=f"{param}.content")}]
    if itype in ("function_call", "custom_tool_call"):
        # Responses assistant tool call -> Chat assistant message with tool_calls.
        call_id = item.get("call_id") or item.get("id") or f"call_{secrets.token_hex(8)}"
        return [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    **({"namespace": item["namespace"]} if item.get("namespace") is not None else {}),
                    "arguments": (
                        json.dumps({"input": item["input"]}, ensure_ascii=False)
                        if itype == "custom_tool_call"
                        else item.get("arguments", "") or ""
                    ),
                },
            }],
        }]
    if itype in ("function_call_output", "custom_tool_call_output"):
        call_id = item.get("call_id") or item.get("id") or ""
        output = item.get("output", "")
        if isinstance(output, (list, dict)):
            # Apply the same attachment validation as message content before
            # retaining the existing JSON representation of structured results.
            _content_to_chat_content(output, param=f"{param}.output")
        if not isinstance(output, str):
            try:
                output = json.dumps(output)
            except Exception:
                output = str(output)
        return [{
            "role": "tool",
            "tool_call_id": call_id,
            "content": output,
        }]
    # Reasoning / refusal / hosted-tool calls / hosted-tool outputs are not
    # useful when replaying through Chat Completions. Skip them.
    return []


def _input_to_messages(input_value: Any) -> list[dict]:
    """Top-level Responses `input` -> list of Chat Completions messages."""
    if input_value is None:
        return []
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, list):
        messages: list[dict] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(input_value):
            if isinstance(item, dict) and "id" in item:
                item_id = item["id"]
                if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
                    raise ResponsesTranslationError(
                        "Input item IDs must be unique, non-empty strings.",
                        param=f"input[{index}].id",
                    )
                seen_ids.add(item_id)
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            else:
                messages.extend(_input_item_to_messages(item, param=f"input[{index}]"))
        return messages
    raise ResponsesTranslationError(
        "Field 'input' must be a string or array of input items.",
        param="input",
    )


class ToolMapping:
    """One request's reversible tool identities, including replay and discovery.

    Chat function names have no namespace and must fit 64 ASCII characters.
    Flat names retain their spelling; namespaced identities get a deterministic
    digest alias. Reserve flat/history names first and resolve any alias clash.
    The mapping stays in the gateway and is shared by both translation directions.
    """

    def __init__(self, req: dict, prior_messages: Optional[list[dict]] = None):
        self.tools: list[dict] = []
        self.identities: dict[tuple[Optional[str], str], str] = {}
        definitions: dict[tuple[Optional[str], str], dict] = {}

        def add_tools(tools: Any, param: str, *, discovered: bool = False) -> None:
            if tools is None:
                return
            if not isinstance(tools, list):
                raise ResponsesTranslationError("Tools must be an array.", param=param)
            for index, tool in enumerate(tools):
                path = f"{param}[{index}]"
                if not isinstance(tool, dict):
                    raise ResponsesTranslationError("Tool must be an object.", param=path)
                if tool.get("type") == "namespace":
                    namespace = tool.get("name")
                    if not isinstance(namespace, str) or not namespace:
                        raise ResponsesTranslationError("Namespace requires a non-empty name.", param=path + ".name")
                    if tool.get("description") is not None and not isinstance(tool["description"], str):
                        raise ResponsesTranslationError("Namespace description must be a string.", param=path + ".description")
                    nested = tool.get("tools")
                    if not isinstance(nested, list):
                        raise ResponsesTranslationError("Namespace tools must be an array.", param=path + ".tools")
                    for child_index, child in enumerate(nested):
                        child_path = f"{path}.tools[{child_index}]"
                        if not isinstance(child, dict) or child.get("type") not in ("function", "custom"):
                            raise ResponsesTranslationError("Namespace members must be function or custom tools.", param=child_path)
                        add(child, namespace, tool.get("description") or "", child_path, discovered)
                else:
                    add(tool, None, "", path, discovered)

        def add(tool: dict, namespace: Optional[str], description: str, path: str, discovered: bool) -> None:
            kind = tool.get("type")
            if kind == "tool_search":
                if tool.get("execution") != "client":
                    raise ResponsesTranslationError(
                        "Azizam supports tool_search with execution='client' only. Codex performs discovery; hosted search is unavailable.",
                        code="hosted_tool_not_supported", param=path + ".execution",
                    )
                name = "tool_search"
            else:
                inner = tool.get("function") if isinstance(tool.get("function"), dict) else {}
                name = tool.get("name") or inner.get("name")
            # Retain the established hosted/unknown-tool errors.
            if kind not in ("function", "custom", "tool_search"):
                try:
                    _translate_tools([tool])
                except ResponsesTranslationError as error:
                    error.param = (error.param or "tools[0]").replace("tools[0]", path, 1)
                    raise
            if not isinstance(name, str) or not name:
                raise ResponsesTranslationError("Tool requires a non-empty name.", param=path + ".name")
            if (namespace is not None or kind == "tool_search") and "parameters" in tool and not isinstance(tool["parameters"], dict):
                raise ResponsesTranslationError("Tool parameter schema must be an object.", param=path + ".parameters")
            identity = (namespace, name)
            if identity in definitions:
                if discovered:
                    return
                if namespace is not None or self.identities[identity] != kind or kind != "function":
                    raise ResponsesTranslationError("Tool identities must be unique.", param=path)
            definitions[identity] = tool
            self.identities[identity] = kind
            self.tools.append({"identity": identity, "tool": tool, "description": description, "path": path})

        add_tools(req.get("tools"), "tools")
        for message in prior_messages or []:
            if "_azizam_discovered_tools" in message:
                add_tools(message["_azizam_discovered_tools"], "previous_response_id.tools", discovered=True)
        input_items = req.get("input") if isinstance(req.get("input"), list) else []
        for index, item in enumerate(input_items):
            if isinstance(item, dict) and item.get("type") == "tool_search_output":
                if item.get("execution") != "client":
                    raise ResponsesTranslationError("Hosted tool_search output is unavailable.", code="hosted_tool_not_supported", param=f"input[{index}].execution")
                add_tools(item.get("tools"), f"input[{index}].tools", discovered=True)
        # Replayed calls need an alias even if their declaration is no longer active.
        history = list(prior_messages or [])
        for item in input_items:
            if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call", "tool_search_call"):
                history.extend(_input_item_to_messages(item))
        for message in history:
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                namespace, name = fn.get("namespace"), fn.get("name", "")
                if namespace is not None and (not isinstance(namespace, str) or not namespace):
                    raise ResponsesTranslationError("Call namespace must be a non-empty string.", param="input")
                if not isinstance(name, str):
                    raise ResponsesTranslationError("Call name must be a string.", param="input")
                self.identities.setdefault((namespace, name), "function")

        reserved = {name for namespace, name in self.identities if namespace is None}
        self.forward: dict[tuple[Optional[str], str], str] = {}
        for identity in sorted(self.identities, key=lambda value: (value[0] or "", value[1])):
            namespace, name = identity
            alias = name
            if namespace is not None:
                salt = 0
                while True:
                    encoded = json.dumps([namespace, name, salt], ensure_ascii=False).encode()
                    alias = "ns__" + hashlib.sha256(encoded).hexdigest()[:60]
                    if alias not in reserved:
                        break
                    salt += 1
                reserved.add(alias)
            self.forward[identity] = alias
        self.reverse = {alias: identity for identity, alias in self.forward.items()}
        self.custom_names = {self.forward[identity] for identity, kind in self.identities.items() if kind == "custom"}
        self.search_names = {self.forward[identity] for identity, kind in self.identities.items() if kind == "tool_search"}
        self.buffer_calls = bool(self.custom_names or self.search_names or any(namespace is not None for namespace, _ in self.identities))

    def chat_tools(self) -> list[dict]:
        result = []
        for entry in self.tools:
            tool = entry["tool"]
            if tool["type"] == "tool_search":
                tool = {**tool, "type": "function", "name": "tool_search"}
            try:
                translated = copy.deepcopy(_translate_tools([tool])[0])
            except ResponsesTranslationError as error:
                error.param = (error.param or "tools[0]").replace("tools[0]", entry["path"], 1)
                raise
            fn = translated["function"]
            fn["name"] = self.forward[entry["identity"]]
            namespace, name = entry["identity"]
            if namespace is not None:
                fn["description"] = f"Namespace: {namespace}. Tool: {name}.\n{entry['description']}\n{fn.get('description', '')}".strip()
            result.append(translated)
        return result

    def chat_messages(self, messages: list[dict]) -> list[dict]:
        messages = copy.deepcopy(messages)
        for message in messages:
            message.pop("_azizam_discovered_tools", None)
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                identity = (fn.pop("namespace", None), fn.get("name", ""))
                fn["name"] = self.forward.get(identity, identity[1])
        return messages


def _translate_tools(tools: Any) -> list[dict]:
    """Translate Responses `tools` -> Chat Completions `tools`.

    Hosted tools are rejected. Custom tools use a reversible string wrapper.
    """
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ResponsesTranslationError(
            "Field 'tools' must be an array.",
            param="tools",
        )
    out: list[dict] = []
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ResponsesTranslationError(
                f"tools[{i}] must be an object.",
                param=f"tools[{i}]",
            )
        ttype = tool.get("type")
        if ttype in HOSTED_TOOL_TYPES:
            raise ResponsesTranslationError(
                f"Hosted tool '{ttype}' is not supported by this proxy. "
                f"Function, custom, namespace and client tool_search tools are accepted; the gateway has no first-party search, "
                f"file search, code interpreter, or computer-use surface.",
                code="hosted_tool_not_supported",
                param=f"tools[{i}].type",
            )
        if ttype == "function":
            # Responses shape (flat):
            #   {type, name, description, parameters, strict}
            # Chat Completions shape (nested):
            #   {type: function, function: {name, description, parameters, strict}}
            name = tool.get("name")
            if not name:
                # Some clients nest function metadata under "function" already.
                inner = tool.get("function") if isinstance(tool.get("function"), dict) else None
                if inner:
                    name = inner.get("name")
                    out.append({"type": "function", "function": inner})
                    continue
                raise ResponsesTranslationError(
                    f"tools[{i}].name is required for function tools.",
                    param=f"tools[{i}].name",
                )
            fn: dict = {"name": name}
            if "description" in tool:
                fn["description"] = tool["description"]
            if "parameters" in tool:
                fn["parameters"] = tool["parameters"]
            if "strict" in tool:
                fn["strict"] = tool["strict"]
            out.append({"type": "function", "function": fn})
            continue
        if ttype == "custom":
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise ResponsesTranslationError("Custom tools require a non-empty name.", param=f"tools[{i}].name")
            fmt = tool.get("format", {"type": "text"})
            if not isinstance(fmt, dict) or fmt.get("type") not in ("text", "grammar"):
                raise ResponsesTranslationError("Custom tool format must be text or grammar.", param=f"tools[{i}].format")
            guidance = "Pass the freeform text verbatim in the string parameter input. Format: text."
            if fmt["type"] == "grammar":
                if fmt.get("syntax") not in ("lark", "regex") or not isinstance(fmt.get("definition"), str):
                    raise ResponsesTranslationError("Grammar format requires lark or regex syntax and a string definition.", param=f"tools[{i}].format")
                guidance = (
                    "Pass the freeform text verbatim in the string parameter input. "
                    f"Format: grammar ({fmt['syntax']}); advisory only, not enforced by Azizam.\n"
                    + fmt["definition"]
                )
            out.append({"type": "function", "function": {
                "name": name,
                "description": ((tool.get("description") or "") + "\n\n" + guidance).strip(),
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}},
                               "required": ["input"], "additionalProperties": False},
            }})
            continue
        raise ResponsesTranslationError(
            f"Unknown tool type '{ttype}' at tools[{i}].",
            param=f"tools[{i}].type",
        )
    names = [tool["function"].get("name") for tool in out]
    custom_names = {tool["name"] for tool in tools if tool.get("type") == "custom"}
    if any(names.count(name) != 1 for name in custom_names):
        raise ResponsesTranslationError("Tool names must be unique to distinguish function and custom calls.", param="tools")
    return out


def _translate_tool_choice(tc: Any) -> Any:
    """Translate Responses tool_choice -> Chat Completions tool_choice.

    Most shapes are identical: "auto", "none", "required", or
    {type: function, name: "..."} (Responses) / {type: function, function: {name: "..."}} (Chat).
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        t = tc.get("type")
        if t in ("function", "custom"):
            name = tc.get("name") or (tc.get("function") or {}).get("name")
            if not name:
                return "auto"
            return {"type": "function", "function": {"name": name}}
        if t == "allowed_tools":
            raise ResponsesTranslationError(
                f"Azizam does not support tool_choice type '{t}'.",
                code="unsupported_operation",
                param="tool_choice",
            )
        return "auto"
    return "auto"


def _translate_text_format(text_cfg: Any) -> Optional[dict]:
    """Translate Responses `text.format` -> Chat Completions `response_format`."""
    if not isinstance(text_cfg, dict):
        return None
    fmt = text_cfg.get("format")
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype == "text":
        return None
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        # Responses shape: {type: json_schema, name, schema, strict, description}
        # Chat Completions shape: {type: json_schema, json_schema: {name, schema, strict, description}}
        js: dict = {}
        for k in ("name", "schema", "strict", "description"):
            if k in fmt:
                js[k] = fmt[k]
        if "name" not in js:
            js["name"] = "response"
        return {"type": "json_schema", "json_schema": js}
    return None


# Effort values that suppress thinking on local reasoning models (Qwen3.6 family,
# binary thinking switch). Keep in sync with proxy._REASONING_EFFORT_SUPPRESS.
# "none" was added to OpenAI's spec with gpt-5.1 (Nov 2025); "minimal" predates it.
_REASONING_EFFORT_SUPPRESS = frozenset({"none", "minimal"})


def _apply_reasoning_effort_messages(messages: list[dict], reasoning: Any) -> list[dict]:
    """Honor reasoning.effort=none/minimal by appending /no_think to the system
    prompt. Llama-server's Qwen3 chat template additionally honors a
    `chat_template_kwargs.enable_thinking=false` flag (set in
    `_apply_reasoning_effort_kwargs`); we set the system-prompt hint as a
    belt-and-suspenders fallback for backends that ignore the kwarg."""
    if not isinstance(reasoning, dict):
        return messages
    if reasoning.get("effort") not in _REASONING_EFFORT_SUPPRESS:
        return messages
    hint = "/no_think"
    out = list(messages)
    for i, m in enumerate(out):
        if m.get("role") == "system":
            existing = m.get("content") or ""
            if hint not in existing:
                out[i] = {**m, "content": (existing + ("\n" if existing else "") + hint)}
            return out
    out.insert(0, {"role": "system", "content": hint})
    return out


def _apply_reasoning_effort_kwargs(cc: dict, reasoning: Any) -> None:
    """If reasoning.effort=none/minimal, request that the chat template suppress
    thinking via the llama-server-recognized `chat_template_kwargs.enable_thinking`
    flag. Other backends will ignore unknown fields."""
    if not isinstance(reasoning, dict):
        return
    if reasoning.get("effort") not in _REASONING_EFFORT_SUPPRESS:
        return
    cc.setdefault("chat_template_kwargs", {})
    if isinstance(cc["chat_template_kwargs"], dict):
        cc["chat_template_kwargs"].setdefault("enable_thinking", False)


def to_chat_completions(
    req: dict,
    *,
    prior_messages: Optional[list[dict]] = None,
    tool_mapping: Optional[ToolMapping] = None,
) -> dict:
    """Translate a Responses request body to a Chat Completions request body.

    `prior_messages` is the reconstructed conversation history from
    previous_response_id (already in Chat Completions shape). It is prepended
    to the new messages.
    """
    if not isinstance(req, dict):
        raise ResponsesTranslationError("Request body must be a JSON object.")
    tool_mapping = tool_mapping or ToolMapping(req, prior_messages)

    for field, what in (
        ("background", "background responses"),
        ("conversation", "the Conversations API"),
        ("prompt", "stored prompt templates"),
    ):
        if req.get(field):
            raise ResponsesTranslationError(
                f"`{field}` is not supported: this gateway does not implement {what}.",
                code="unsupported_operation",
                param=field,
            )

    messages: list[dict] = []
    if prior_messages:
        messages.extend(prior_messages)

    # `instructions` is the Responses-specific system message. It overrides
    # any system message inherited from the prior chain (per the OpenAI docs:
    # "instructions from a previous response will not be carried over").
    instructions = req.get("instructions")
    if isinstance(instructions, str) and instructions:
        # Drop any leading system messages inherited from prior_messages so the
        # new instructions take precedence.
        messages = [m for m in messages if m.get("role") != "system"]
        messages.insert(0, {"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        # Less common: instructions can be a list of input items in newer
        # Stainless schemas. Flatten to text.
        text = _coerce_content_to_text(instructions)
        if text:
            messages = [m for m in messages if m.get("role") != "system"]
            messages.insert(0, {"role": "system", "content": text})

    messages.extend(_input_to_messages(req.get("input")))

    # reasoning.effort=minimal hint (system-prompt + chat_template_kwargs)
    messages = _apply_reasoning_effort_messages(messages, req.get("reasoning"))

    cc: dict = {
        "model": req.get("model") or "default",
        "messages": tool_mapping.chat_messages(messages),
    }

    # Streaming. Ask for the usage chunk so the final response reports tokens.
    if req.get("stream"):
        cc["stream"] = True
        cc["stream_options"] = {"include_usage": True}

    # Sampling
    for k_in, k_out in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("parallel_tool_calls", "parallel_tool_calls"),
        ("seed", "seed"),
        ("metadata", "metadata"),
        ("user", "user"),
    ):
        if k_in in req and req[k_in] is not None:
            cc[k_out] = req[k_in]

    # Normalize both Responses aggregate-output spellings before model-scoped
    # request transforms run. Keep max_output_tokens authoritative when both
    # are present, preserving the established Responses spelling's behavior.
    if req.get("max_output_tokens") is not None:
        cc["max_tokens"] = req["max_output_tokens"]
    elif req.get("max_completion_tokens") is not None:
        cc["max_tokens"] = req["max_completion_tokens"]

    # Tools
    tools = tool_mapping.chat_tools()
    if tools:
        cc["tools"] = tools

    tc = _translate_tool_choice(req.get("tool_choice"))
    if isinstance(req.get("tool_choice"), dict) and req["tool_choice"].get("type") == "tool_search":
        if not any(entry["tool"]["type"] == "tool_search" for entry in tool_mapping.tools):
            raise ResponsesTranslationError("tool_search choice requires a client tool_search declaration.", param="tool_choice")
        tc = {"type": "function", "function": {"name": next(iter(tool_mapping.search_names))}}
    if tc is not None:
        if isinstance(tc, dict) and tc.get("type") == "function":
            choice = req.get("tool_choice") or {}
            if choice.get("namespace") is not None and (not isinstance(choice["namespace"], str) or not choice["namespace"]):
                raise ResponsesTranslationError("Tool choice namespace must be a non-empty string.", param="tool_choice.namespace")
            identity = (choice.get("namespace"), tc["function"]["name"])
            if identity[0] is not None and not any(entry["identity"] == identity for entry in tool_mapping.tools):
                raise ResponsesTranslationError("Namespaced tool choice must name a declared tool.", param="tool_choice")
            tc["function"]["name"] = tool_mapping.forward.get(identity, identity[1])
        cc["tool_choice"] = tc

    # text.format -> response_format
    rf = _translate_text_format(req.get("text"))
    if rf is not None:
        cc["response_format"] = rf

    # stop sequences (Responses doesn't have a direct field, but some
    # clients pass it).
    if "stop" in req:
        cc["stop"] = req["stop"]

    # Backend hint for thinking-capable models when reasoning.effort=minimal.
    _apply_reasoning_effort_kwargs(cc, req.get("reasoning"))
    reasoning = req.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if effort in ("low", "medium", "high"):
        cc["reasoning_effort"] = effort

    return cc


def _echo_tools(tools: Any) -> list:
    """Echo request tools in the shape the Response schema requires.

    A function tool always carries ``strict`` and ``parameters`` in a Response,
    even when the request left them out.
    """
    out: list = []
    for tool in tools if isinstance(tools, list) else []:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            tool = {**tool, "description": tool.get("description", ""), "tools": _echo_tools(tool.get("tools"))}
        if isinstance(tool, dict) and tool.get("type") == "function":
            tool = {
                **tool,
                "strict": tool.get("strict"),
                "parameters": tool.get("parameters"),
            }
        out.append(tool)
    return out


def _echo_fields(req: dict) -> dict:
    """Request settings that every Response object repeats back."""
    return {
        "instructions": req.get("instructions"),
        "max_output_tokens": req.get("max_output_tokens"),
        "parallel_tool_calls": req.get("parallel_tool_calls", True),
        "previous_response_id": req.get("previous_response_id"),
        "reasoning": req.get("reasoning") or {"effort": None, "summary": None},
        "store": req.get("store") is not False,
        "temperature": req.get("temperature"),
        "text": req.get("text") or {"format": {"type": "text"}},
        "tool_choice": req.get("tool_choice") or "auto",
        "tools": _echo_tools(req.get("tools")),
        "top_p": req.get("top_p"),
        "truncation": req.get("truncation") or "disabled",
        "user": req.get("user"),
        "metadata": req.get("metadata") or {},
        "access_programs": None,
    }


def _output_text_part(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": [], "logprobs": []}


def _reasoning_item(item_id: str, text: str, status: str = "completed") -> dict:
    return {
        "id": item_id,
        "type": "reasoning",
        "status": status,
        "summary": [],
        "content": [{"type": "reasoning_text", "text": text}] if text else [],
    }


def _custom_tool_input(arguments: str) -> str:
    """Decode only the advertised wrapper, never guess at a malformed tool call."""
    try:
        value = json.loads(arguments)
    except (ValueError, TypeError) as error:
        raise ValueError("Upstream custom tool arguments must be a JSON object with one string input field.") from error
    if not isinstance(value, dict) or set(value) != {"input"} or not isinstance(value["input"], str):
        raise ValueError("Upstream custom tool arguments must be a JSON object with one string input field.")
    return value["input"]


def _tool_call_item(*, item_id: str, call_id: str, name: str, arguments: str,
                    custom_names: set[str], status: str = "completed", tool_mapping: Optional[ToolMapping] = None) -> dict:
    if tool_mapping is not None and name in tool_mapping.search_names:
        try:
            value = json.loads(arguments) if status == "completed" else {}
        except (ValueError, TypeError) as error:
            raise ValueError("Upstream tool_search arguments must be a JSON object.") from error
        if not isinstance(value, dict):
            raise ValueError("Upstream tool_search arguments must be a JSON object.")
        return {"id": item_id, "type": "tool_search_call", "call_id": call_id,
                "status": status, "execution": "client", "arguments": value}
    custom = name in custom_names
    namespace, original_name = tool_mapping.reverse.get(name, (None, name)) if tool_mapping else (None, name)
    return {
        "id": item_id,
        "type": "custom_tool_call" if custom else "function_call",
        **({} if custom else {"status": status}),
        "call_id": call_id,
        "name": original_name,
        **({"namespace": namespace} if namespace is not None else {}),
        **({"input": _custom_tool_input(arguments) if status == "completed" else ""}
           if custom else {"arguments": arguments}),
    }


def _build_output_items_from_message(msg: dict, custom_names: set[str], tool_mapping: Optional[ToolMapping] = None) -> list[dict]:
    """Map a Chat Completions assistant message to Responses output items.

    llama-server returns reasoning in a separate `reasoning_content` field
    (vs OpenAI's first-party hosted reasoning models which produce reasoning
    inline). Surface it as a Responses `reasoning` output item so clients
    that inspect the output array see the model actually thought, even when
    `content` is empty (e.g. truncated at max_output_tokens during thinking).
    """
    items: list[dict] = []
    reasoning_content = msg.get("reasoning_content") or msg.get("reasoning")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    if isinstance(reasoning_content, str) and reasoning_content:
        items.append(_reasoning_item("rs_" + secrets.token_hex(12), reasoning_content))
    message_content: list[dict] = []
    if content:
        text = content if isinstance(content, str) else _coerce_content_to_text(content)
        message_content.append(_output_text_part(text))
    refusal = msg.get("refusal")
    if isinstance(refusal, str) and refusal:
        message_content.append({"type": "refusal", "refusal": refusal})
    if message_content:
        items.append({
            "id": "msg_" + secrets.token_hex(12),
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": message_content,
        })
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        items.append(_tool_call_item(
            item_id=("ctc_" if name in custom_names else "fc_") + secrets.token_hex(12),
            call_id=tc.get("id") or ("call_" + secrets.token_hex(8)),
            name=name, arguments=fn.get("arguments", "") or "", custom_names=custom_names, tool_mapping=tool_mapping,
        ))
    return items


def _flatten_output_text(output: list[dict]) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    txt = c.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
    return "".join(parts)


def _translate_usage(usage: Any) -> dict:
    """Chat Completions usage -> Responses usage."""
    if not isinstance(usage, dict):
        usage = {}
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    tt = int(usage.get("total_tokens", pt + ct) or 0)
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cached = (prompt_details or {}).get("cached_tokens") if isinstance(prompt_details, dict) else 0
    reasoning = (
        (completion_details or {}).get("reasoning_tokens")
        if isinstance(completion_details, dict)
        else 0
    )
    return {
        "input_tokens": pt,
        "input_tokens_details": {
            "cached_tokens": int(cached or 0),
            "cache_write_tokens": 0,
        },
        "output_tokens": ct,
        "output_tokens_details": {"reasoning_tokens": int(reasoning or 0)},
        "total_tokens": tt,
    }


def from_chat_completions(
    cc_resp: dict,
    *,
    response_id: str,
    created_at: int,
    original_request: dict,
    tool_mapping: Optional[ToolMapping] = None,
) -> dict:
    """Translate a Chat Completions response body to a Responses response body."""
    choices = cc_resp.get("choices") or []
    first = choices[0] if choices else {}
    msg = first.get("message") or {}
    tool_mapping = tool_mapping or ToolMapping(original_request)
    output = _build_output_items_from_message(msg, tool_mapping.custom_names, tool_mapping)

    finish_reason = first.get("finish_reason")
    status = "completed"
    incomplete_details = None
    if finish_reason == "length":
        status = "incomplete"
        incomplete_details = {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        status = "incomplete"
        incomplete_details = {"reason": "content_filter"}

    model = cc_resp.get("model") or original_request.get("model") or "default"

    out: dict = {
        "id": response_id,
        "object": "response",
        "created_at": int(created_at),
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
        "model": model,
        "output": output,
        "output_text": _flatten_output_text(output),
        "usage": _translate_usage(cc_resp.get("usage")),
        **_echo_fields(original_request),
    }
    return out


def input_items_from_request(req: dict, *, response_id: str = "") -> list[dict]:
    """The ``input`` of a stored request as Responses input items, in order."""
    value = req.get("input")
    if isinstance(value, str):
        value = [{"role": "user", "content": value}]
    items: list[dict] = []
    for position, item in enumerate(value if isinstance(value, list) else []):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        if item.get("type") in (None, "message"):
            item["type"] = "message"
            content = item.get("content")
            if isinstance(content, str):
                role = item.get("role")
                kind = "output_text" if role == "assistant" else "input_text"
                item["content"] = [{"type": kind, "text": content}]
        # IDs must survive repeated reads so an `after` cursor remains usable.
        identity = f"{response_id}:{position}:{json.dumps(item, sort_keys=True, ensure_ascii=False)}"
        item.setdefault("id", "item_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24])
        items.append(item)
    return items


def stored_chain_to_messages(chain: list[dict]) -> list[dict]:
    """Given a chain of stored response rows (oldest first), reconstruct
    the Chat Completions `messages` array that would re-create the
    conversation context for the next call.

    For each row we re-translate its original `input` to messages and append
    the assistant output as message(s)/tool-call(s).
    """
    messages: list[dict] = []
    for row in chain:
        req = row.get("request_json") or {}
        resp = row.get("response_json") or {}

        # Inputs for this turn — but DO NOT re-prepend `instructions`; only
        # the latest call's `instructions` (or current call's) should apply.
        # We still need to walk input items as user/tool messages.
        turn_messages = _input_to_messages(req.get("input"))
        messages.extend(turn_messages)

        # Assistant output from this turn.
        for item in resp.get("output") or []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                text = _coerce_content_to_text(item.get("content"))
                messages.append({"role": "assistant", "content": text})
            elif itype in ("function_call", "custom_tool_call", "tool_search_call", "tool_search_output"):
                messages.extend(_input_item_to_messages(item))
    return messages


# -------- Streaming translation --------------------------------------------

def sse_pack(event: str, data: dict) -> bytes:
    """Format one SSE event."""
    return (f"event: {event}\n" + f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


def make_skeleton_response(
    *,
    response_id: str,
    created_at: int,
    original_request: dict,
    status: str,
) -> dict:
    """Build a Responses object with empty output, used in `response.created`
    and `response.in_progress` events."""
    model = original_request.get("model") or "default"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(created_at),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": [],
        "output_text": "",
        "usage": None,
        **_echo_fields(original_request),
    }


class StreamTranslator:
    """Consume Chat Completions SSE chunks; emit Responses SSE events.

    Usage:
        st = StreamTranslator(response_id=..., created_at=..., original_request=...)
        yield from st.start()              # emits response.created + response.in_progress
        for cc_line in upstream_sse_lines:
            for evt in st.feed(cc_line):
                yield evt
        yield from st.finish()              # emits remaining done events + response.completed

    The translator tracks output-item indexes so deltas can reference them.
    """

    def __init__(self, *, response_id: str, created_at: int, original_request: dict, tool_mapping: Optional[ToolMapping] = None):
        self.response_id = response_id
        self.created_at = int(created_at)
        self.original_request = original_request
        self._tool_mapping = tool_mapping or ToolMapping(original_request)
        self._custom_names = self._tool_mapping.custom_names
        self._buffer_calls = self._tool_mapping.buffer_calls
        self._seq = 0
        self._started = False
        self._buf = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._pending_cr = ""
        self._upstream_done = False
        self._error: Optional[dict] = None
        self._finished = False

        # Output bookkeeping
        self._reasoning: Optional[dict] = None  # index, id, text parts, closed
        self._message_index: Optional[int] = None
        self._message_id: Optional[str] = None
        self._message_text: list[str] = []
        self._message_parts: dict[str, list[str]] = {}
        # Tool calls keyed by their streaming index (Chat Completions convention).
        self._tool_calls: dict[int, dict] = {}
        # Output ordering — list of (kind, key) so we can serialize at the end.
        self._output_order: list[tuple] = []

        self._finish_reason: Optional[str] = None
        self._usage: Optional[dict] = None
        self._model_seen: Optional[str] = None

        self._done_sent = False

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def start(self) -> list[bytes]:
        """Emit the `response.created` and `response.in_progress` events."""
        if self._started:
            return []
        self._started = True
        skel = make_skeleton_response(
            response_id=self.response_id,
            created_at=self.created_at,
            original_request=self.original_request,
            status="in_progress",
        )
        ev1 = sse_pack("response.created", {
            "type": "response.created",
            "sequence_number": self._next_seq(),
            "response": skel,
        })
        ev2 = sse_pack("response.in_progress", {
            "type": "response.in_progress",
            "sequence_number": self._next_seq(),
            "response": skel,
        })
        return [ev1, ev2]

    def feed(self, line_bytes: bytes) -> list[bytes]:
        """Feed one byte chunk from the upstream SSE stream. Returns
        zero-or-more translated Responses SSE events."""
        if self._finished or self._error or self._upstream_done:
            return []
        return self._feed_text(self._decoder.decode(line_bytes))

    def _feed_text(self, text: str, *, final: bool = False) -> list[bytes]:
        if self._upstream_done or self._error:
            return []
        # Hold a trailing CR until the next chunk distinguishes CR from CRLF.
        text = self._pending_cr + text
        self._pending_cr = ""
        if text.endswith("\r") and not final:
            text, self._pending_cr = text[:-1], "\r"
        self._buf += text.replace("\r\n", "\n").replace("\r", "\n")

        events: list[bytes] = []
        # Process complete SSE messages (terminated by blank line).
        while "\n\n" in self._buf:
            raw, self._buf = self._buf.split("\n\n", 1)
            data_lines: list[str] = []
            for ln in raw.split("\n"):
                if ln.startswith("data:"):
                    data_lines.append(ln[5:].lstrip())
            if not data_lines:
                continue
            payload = "\n".join(data_lines).strip()
            if payload == "[DONE]":
                self._upstream_done = True
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            events.extend(self._handle_cc_chunk(chunk))
            if self._error:
                break
        return events

    def fail(self, message: str, *, code: str = "server_error", param: Optional[str] = None) -> list[bytes]:
        """Retain failure state and emit a documented flat error event."""
        if self._done_sent or self._error:
            return []
        # Response.error has a narrower code enum than the flat error event.
        response_code = "rate_limit_exceeded" if code == "rate_limit_exceeded" else "server_error"
        self._error = {"code": response_code, "message": message}
        return [sse_pack("error", {
            "type": "error",
            "sequence_number": self._next_seq(),
            "message": message,
            "code": code,
            "param": param,
        })]

    def _reasoning_delta(self, text: str) -> list[bytes]:
        events: list[bytes] = []
        if self._reasoning is None:
            idx = len(self._output_order)
            self._reasoning = {
                "index": idx,
                "id": "rs_" + secrets.token_hex(12),
                "text": [],
                "closed": False,
            }
            self._output_order.append(("reasoning", idx))
            events.append(sse_pack("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": idx,
                "item": _reasoning_item(self._reasoning["id"], "", "in_progress"),
            }))
        r = self._reasoning
        r["text"].append(text)
        events.append(sse_pack("response.reasoning_text.delta", {
            "type": "response.reasoning_text.delta",
            "sequence_number": self._next_seq(),
            "item_id": r["id"],
            "output_index": r["index"],
            "content_index": 0,
            "delta": text,
        }))
        return events

    def _close_reasoning(self) -> list[bytes]:
        r = self._reasoning
        if r is None or r["closed"]:
            return []
        r["closed"] = True
        full = "".join(r["text"])
        return [
            sse_pack("response.reasoning_text.done", {
                "type": "response.reasoning_text.done",
                "sequence_number": self._next_seq(),
                "item_id": r["id"],
                "output_index": r["index"],
                "content_index": 0,
                "text": full,
            }),
            sse_pack("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": r["index"],
                "item": _reasoning_item(r["id"], full),
            }),
        ]

    def _ensure_message_item(self, part_type: str = "output_text") -> tuple[list[bytes], int, str]:
        out_events: list[bytes] = self._close_reasoning()
        if self._message_index is None:
            idx = len(self._output_order)
            mid = "msg_" + secrets.token_hex(12)
            self._message_index = idx
            self._message_id = mid
            self._output_order.append(("message", idx))
            item = {
                "id": mid,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            out_events.append(sse_pack("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": idx,
                "item": item,
            }))
        if part_type not in self._message_parts:
            content_index = len(self._message_parts)
            self._message_parts[part_type] = []
            part = _output_text_part("") if part_type == "output_text" else {"type": "refusal", "refusal": ""}
            out_events.append(sse_pack("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self._next_seq(),
                "output_index": self._message_index,
                "item_id": self._message_id,
                "content_index": content_index,
                "part": part,
            }))
        return out_events, self._message_index, self._message_id  # type: ignore[return-value]

    def _ensure_tool_call_item(self, tc_index: int, tc_id: Optional[str], name: Optional[str]) -> tuple[list[bytes], dict]:
        out_events: list[bytes] = self._close_reasoning()
        if self._buffer_calls:
            # Buffer mixed tool calls until names and JSON wrappers are complete.
            # A name can itself arrive in fragments; announcing it early could
            # incorrectly commit a custom call to the function-call wire shape.
            if tc_index not in self._tool_calls:
                self._tool_calls[tc_index] = {
                    "item_id": "tc_" + secrets.token_hex(12),
                    "call_id": tc_id or ("call_" + secrets.token_hex(8)),
                    "name": "", "arguments": "",
                }
            entry = self._tool_calls[tc_index]
            if tc_id:
                entry["call_id"] = tc_id
            if name:
                entry["name"] += name
            return out_events, entry
        if tc_index not in self._tool_calls:
            order_idx = len(self._output_order)
            fc_id = "fc_" + secrets.token_hex(12)
            call_id = tc_id or ("call_" + secrets.token_hex(8))
            entry = {
                "order_index": order_idx,
                "item_id": fc_id,
                "call_id": call_id,
                "name": name or "",
                "arguments": "",
            }
            self._tool_calls[tc_index] = entry
            self._output_order.append(("function_call", tc_index))
            item = {
                "id": fc_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name or "",
                "arguments": "",
            }
            out_events.append(sse_pack("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self._next_seq(),
                "output_index": order_idx,
                "item": item,
            }))
        else:
            entry = self._tool_calls[tc_index]
            # Late-arriving id/name should fill in.
            if tc_id and not entry["call_id"].startswith("call_"):
                entry["call_id"] = tc_id
            elif tc_id:
                entry["call_id"] = tc_id
            if name and not entry["name"]:
                entry["name"] = name
        return out_events, self._tool_calls[tc_index]

    @staticmethod
    def _valid_cc_chunk(chunk: dict) -> bool:
        """Validate every consumed nested value before any stream mutation."""
        usage = chunk.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                return False
            try:
                _translate_usage(usage)
            except (TypeError, ValueError, OverflowError):
                return False
        choices = chunk.get("choices")
        if choices is None:
            return True
        if not isinstance(choices, list):
            return False
        for choice in choices:
            if not isinstance(choice, dict):
                return False
            if choice.get("finish_reason") is not None and not isinstance(choice["finish_reason"], str):
                return False
            delta = choice.get("delta")
            if delta is None:
                continue
            if not isinstance(delta, dict):
                return False
            for key in ("reasoning_content", "reasoning", "content", "refusal"):
                if delta.get(key) is not None and not isinstance(delta[key], str):
                    return False
            calls = delta.get("tool_calls")
            if calls is None:
                continue
            if not isinstance(calls, list):
                return False
            for call in calls:
                if not isinstance(call, dict):
                    return False
                index = call.get("index", 0)
                if type(index) is not int or index < 0:
                    return False
                if call.get("id") is not None and not isinstance(call["id"], str):
                    return False
                function = call.get("function")
                if function is None:
                    continue
                if not isinstance(function, dict):
                    return False
                for key in ("name", "arguments"):
                    if function.get(key) is not None and not isinstance(function[key], str):
                        return False
        return True

    def _handle_cc_chunk(self, chunk: dict) -> list[bytes]:
        events: list[bytes] = []
        if not isinstance(chunk, dict):
            return self.fail("Malformed upstream stream frame.")
        if chunk.get("error") is not None:
            error = chunk["error"]
            if isinstance(error, dict):
                return self.fail(str(error.get("message") or "Upstream stream failed."),
                                 code=str(error.get("code") or "server_error"), param=error.get("param"))
            return self.fail(str(error))
        if not self._valid_cc_chunk(chunk):
            return self.fail("Malformed upstream stream frame.")
        if "model" in chunk and isinstance(chunk["model"], str):
            self._model_seen = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self._usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return events
        choice = choices[0]
        if not isinstance(choice, dict):
            return events
        delta = choice.get("delta") or {}
        # Reasoning delta (llama-server: reasoning_content)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.extend(self._reasoning_delta(reasoning))
        # Text delta
        content = delta.get("content")
        if isinstance(content, str) and content:
            ev, idx, mid = self._ensure_message_item()
            events.extend(ev)
            self._message_text.append(content)
            self._message_parts["output_text"].append(content)
            events.append(sse_pack("response.output_text.delta", {
                "type": "response.output_text.delta",
                "sequence_number": self._next_seq(),
                "item_id": mid,
                "output_index": idx,
                "content_index": list(self._message_parts).index("output_text"),
                "delta": content,
                "logprobs": [],
            }))
        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            ev, idx, mid = self._ensure_message_item("refusal")
            events.extend(ev)
            self._message_parts["refusal"].append(refusal)
            events.append(sse_pack("response.refusal.delta", {
                "type": "response.refusal.delta",
                "sequence_number": self._next_seq(),
                "item_id": mid,
                "output_index": idx,
                "content_index": list(self._message_parts).index("refusal"),
                "delta": refusal,
            }))
        # Tool-call deltas (streaming function-call args).
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_index = tc.get("index", 0)
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                name = fn.get("name")
                args_chunk = fn.get("arguments") or ""
                ev, entry = self._ensure_tool_call_item(tc_index, tc_id, name)
                events.extend(ev)
                if args_chunk:
                    entry["arguments"] += args_chunk
                    if self._buffer_calls:
                        continue
                    events.append(sse_pack("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": self._next_seq(),
                        "item_id": entry["item_id"],
                        "output_index": entry["order_index"],
                        "delta": args_chunk,
                    }))
        fr = choice.get("finish_reason")
        if fr:
            self._finish_reason = fr
        return events

    def _message_content(self) -> list[dict]:
        return [
            _output_text_part("".join(parts)) if kind == "output_text"
            else {"type": "refusal", "refusal": "".join(parts)}
            for kind, parts in self._message_parts.items()
        ]

    def finish(self, *, emit_terminal: bool = True) -> list[bytes]:
        """Close output items, optionally deferring the terminal event until storage succeeds."""
        if self._finished:
            return []
        events: list[bytes] = []
        if not self._error:
            try:
                events.extend(self._feed_text(self._decoder.decode(b"", final=True), final=True))
            except UnicodeDecodeError:
                events.extend(self.fail("Upstream stream ended with incomplete UTF-8."))
        if not self._error and self._finish_reason is None:
            events.extend(self.fail("Upstream stream ended without a finish_reason."))
        if self._buffer_calls and not self._error:
            try:
                for entry in self._tool_calls.values():
                    entry["item"] = _tool_call_item(
                        item_id=entry["item_id"], call_id=entry["call_id"], name=entry["name"],
                        arguments=entry["arguments"], custom_names=self._custom_names, tool_mapping=self._tool_mapping,
                    )
            except ValueError as error:
                events.extend(self.fail(str(error), code="upstream_error"))
        self._finished = True
        events.extend(self._close_reasoning())

        # Close message item if any.
        if self._message_index is not None and self._message_id is not None:
            for content_index, part in enumerate(self._message_content()):
                refusal = part["type"] == "refusal"
                event_type = "response.refusal.done" if refusal else "response.output_text.done"
                value_field = "refusal" if refusal else "text"
                events.append(sse_pack(event_type, {
                    "type": event_type,
                    "sequence_number": self._next_seq(),
                    "item_id": self._message_id,
                    "output_index": self._message_index,
                    "content_index": content_index,
                    value_field: part[value_field],
                    **({} if refusal else {"logprobs": []}),
                }))
                events.append(sse_pack("response.content_part.done", {
                    "type": "response.content_part.done",
                    "sequence_number": self._next_seq(),
                    "output_index": self._message_index,
                    "item_id": self._message_id,
                    "content_index": content_index,
                    "part": part,
                }))
            events.append(sse_pack("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": self._message_index,
                "item": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": self._message_content(),
                },
            }))

        # Close tool-call items.
        for tc_index, entry in self._tool_calls.items():
            if self._buffer_calls and self._error:
                continue
            item = entry.get("item") or _tool_call_item(
                item_id=entry["item_id"], call_id=entry["call_id"], name=entry["name"],
                arguments=entry["arguments"], custom_names=self._custom_names, tool_mapping=self._tool_mapping,
            )
            if item["type"] == "tool_search_call":
                entry["order_index"] = len(self._output_order)
                self._output_order.append(("function_call", tc_index))
                events.append(sse_pack("response.output_item.added", {
                    "type": "response.output_item.added", "sequence_number": self._next_seq(),
                    "output_index": entry["order_index"],
                    "item": _tool_call_item(
                        item_id=entry["item_id"], call_id=entry["call_id"], name=entry["name"],
                        arguments="", custom_names=self._custom_names, tool_mapping=self._tool_mapping, status="in_progress",
                    ),
                }))
                events.append(sse_pack("response.output_item.done", {
                    "type": "response.output_item.done", "sequence_number": self._next_seq(),
                    "output_index": entry["order_index"], "item": item,
                }))
                entry["emitted"] = True
                continue
            custom = item["type"] == "custom_tool_call"
            event_prefix = "response.custom_tool_call_input" if custom else "response.function_call_arguments"
            field = "input" if custom else "arguments"
            if self._buffer_calls:
                # Do not reserve an output slot before its added event. SDK
                # accumulators append items and index subsequent deltas directly.
                entry["order_index"] = len(self._output_order)
                self._output_order.append(("function_call", tc_index))
                events.append(sse_pack("response.output_item.added", {
                    "type": "response.output_item.added",
                    "sequence_number": self._next_seq(),
                    "output_index": entry["order_index"],
                    "item": _tool_call_item(
                        item_id=entry["item_id"], call_id=entry["call_id"], name=entry["name"],
                        arguments="", custom_names=self._custom_names, tool_mapping=self._tool_mapping, status="in_progress",
                    ),
                }))
                if item[field]:
                    events.append(sse_pack(event_prefix + ".delta", {
                        "type": event_prefix + ".delta", "sequence_number": self._next_seq(),
                        "item_id": entry["item_id"], "output_index": entry["order_index"],
                        "delta": item[field],
                    }))
            events.append(sse_pack(event_prefix + ".done", {
                "type": event_prefix + ".done",
                "sequence_number": self._next_seq(),
                "item_id": entry["item_id"],
                "output_index": entry["order_index"],
                field: item[field],
            }))
            events.append(sse_pack("response.output_item.done", {
                "type": "response.output_item.done",
                "sequence_number": self._next_seq(),
                "output_index": entry["order_index"],
                "item": item,
            }))
            entry["emitted"] = True

        if emit_terminal:
            events.extend(self.terminal_event())
        return events

    def terminal_event(self) -> list[bytes]:
        """Emit the final lifecycle event once, after output closure and persistence."""
        if self._done_sent:
            return []
        self._done_sent = True
        final = self.build_final_response()
        terminal = "response." + final["status"]
        return [sse_pack(terminal, {
            "type": terminal,
            "sequence_number": self._next_seq(),
            "response": final,
        })]

    def build_final_response(self) -> dict:
        """Build the full Responses object that the streaming summary represents.
        Used for the terminal lifecycle event AND for persisting to
        responses_store."""
        # Reassemble output in insertion order.
        output: list[dict] = []
        full_text = "".join(self._message_text)
        for kind, key in self._output_order:
            if kind == "reasoning":
                if self._reasoning is not None:
                    output.append(_reasoning_item(
                        self._reasoning["id"], "".join(self._reasoning["text"])
                    ))
            elif kind == "message":
                if self._message_id is None:
                    continue
                output.append({
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": self._message_content(),
                })
            elif kind == "function_call":
                entry = self._tool_calls.get(key)
                if not entry or (self._buffer_calls and not entry.get("emitted")):
                    continue
                output.append(entry.get("item") or _tool_call_item(
                    item_id=entry["item_id"], call_id=entry["call_id"], name=entry["name"],
                    arguments=entry["arguments"], custom_names=self._custom_names, tool_mapping=self._tool_mapping,
                ))

        status = "completed"
        incomplete_details = None
        if self._error:
            status = "failed"
        elif self._finish_reason == "length":
            status = "incomplete"
            incomplete_details = {"reason": "max_output_tokens"}
        elif self._finish_reason == "content_filter":
            status = "incomplete"
            incomplete_details = {"reason": "content_filter"}

        model = self._model_seen or self.original_request.get("model") or "default"
        usage = _translate_usage(self._usage)
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": self._error,
            "incomplete_details": incomplete_details,
            "model": model,
            "output": output,
            "output_text": full_text,
            "usage": usage,
            **_echo_fields(self.original_request),
        }
