"""Small helpers shared by browser-facing routes."""


def with_query(destination: str, query: str) -> str:
    if not query:
        return destination
    return f"{destination}{'&' if '?' in destination else '?'}{query}"


def accepts_html(accept: str) -> bool:
    """Explicit HTML consent; wildcard API clients retain their old response."""
    for item in accept.lower().split(","):
        media_type, *parameters = item.split(";")
        if media_type.strip() != "text/html":
            continue
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name == "q" and separator:
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if 0 < quality <= 1:
            return True
    return False
