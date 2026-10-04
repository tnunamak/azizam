"""Compatibility destinations for the single product UI.

Only known browser pages are translated; API paths and unknown pages stay
outside this map. Query text is retained verbatim, with the selected Access
plans tab taking precedence over any older tab parameter.
"""

ADMIN_SECTIONS = {
    "": "/ui/home/",
    "overview": "/ui/home/",
    "services": "/ui/engines/",
    "status": "/ui/status/",
    "apps": "/ui/apps/",
    "profiles": "/ui/people/?tab=plans",
    "users": "/ui/people/",
    "audit": "/ui/activity/",
    "settings": "/ui/settings/",
}

OWNER_UI_PAGES = frozenset({
    "engines", "providers", "people", "activity", "settings",
})


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


def admin_destination(path: str, query: str = "") -> str | None:
    for prefix in ("/ui/admin", "/admin"):
        if path == prefix or path.startswith(prefix + "/"):
            section = path[len(prefix):].strip("/")
            destination = ADMIN_SECTIONS.get(section)
            return with_query(destination, query) if destination else None
    return None
