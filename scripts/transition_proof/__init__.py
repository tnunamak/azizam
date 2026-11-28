"""Gated park/wake proof harness (docs/design/SERVICE_TRANSITION_GATE.md, "Operation interface").

It drives only the gateway's HTTP surface and names no engine: the domain, the model alias and the frozen
request bodies are arguments, and the way to read "the cached prefix was kept" is data in the case manifest.
"""
