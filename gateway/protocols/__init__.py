"""Pure protocol-frontend translators.

These modules translate between vendor-shaped requests/responses and the
gateway's canonical OpenAI chat-completions-like internal shape. They are
pure: no FastAPI, no httpx, no ``proxy.py`` imports. Live routing wires
them in elsewhere.
"""
