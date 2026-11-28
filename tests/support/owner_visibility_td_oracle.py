"""Reviewer td_oracle.py predicates, TD §2 at c6968dc8. No production imports."""

def is_self(v, r):
    return bool(v.person_id) and bool(r.person_id) and r.person_id == v.person_id


def own_output(v, r, origin):
    """origin: 'app_key_attributed' | 'legacy' for rows without a connection id."""
    if v.delegation == "mcp_grant":
        return bool(v.connection_id) and r.connection_id == v.connection_id
    if v.delegation == "app_key":
        if r.connection_id:  # MCP-created row is not this App key's output
            return False
        return origin == "app_key_attributed" and bool(v.app_id) and r.app_id == v.app_id
    return False


def content_read(v, r, origin):
    if r.person_id is None:
        return (not v.person_id and v.delegation == "app_key" and bool(v.app_id) and r.app_id == v.app_id)
    if not is_self(v, r):
        return False
    return v.delegation is None or own_output(v, r, origin) or v.privacy_permission


def maker_read(v, person, app_id, origin):
    """One maker reads her row (self, with session, permission or the exact origin); an App-less row is read by its App's credential."""
    if person is None:
        return (not v.person_id and v.delegation == "app_key" and bool(v.app_id) and app_id == v.app_id)
    if not (v.person_id and person == v.person_id):
        return False
    if v.delegation is None or v.privacy_permission:
        return True
    if v.delegation == "mcp_grant":
        return origin is not None and origin.auth_kind == "mcp_grant" and bool(v.connection_id) and origin.connection_id == v.connection_id
    if v.delegation == "app_key":
        return origin is not None and origin.auth_kind == "app_key" and bool(origin.credential_id) and bool(v.app_id) and app_id == v.app_id
    return False


def share_hit_legacy(v, r):
    return any(s.active and s.share_id and s.maker_person_id and v.person_id and s.recipient_person_id == v.person_id
               and r.asset_id in (s.asset_id, s.poster_asset_id) for s in r.shares)


def expected(v, r, action, origin):
    if not r.available:
        return False
    k = r.kind
    if k in ("creation", "voice", "receipt", "job"):
        # D51/D52 maker basis: owning an App is not making its content.
        makers = tuple(r.maker_person_ids) if k == "creation" else ((r.person_id,) if r.person_id else ())
        if k == "creation" and len(makers) > 1:
            return action in ("read", "link") and share_hit_legacy(v, r)
        read = maker_read(v, makers[0] if makers else None, r.app_id, getattr(r, "origin", None))
        if k == "creation":
            if action in ("read", "link"):
                return read or share_hit_legacy(v, r)
            if action in ("delete", "reuse"):
                return read
            if action == "share":
                return bool(v.person_id) and makers == (v.person_id,) and (v.delegation is None or v.privacy_permission)
            return False
        if k == "voice":
            return read and action in ("read", "delete", "reuse")
        if k == "receipt":
            return read and action == "read"
        return read and action in ("read", "cancel")
    if k == "prompt":
        if action == "read":
            share = any(s.active and s.share_id and s.include_prompt and s.maker_person_id and v.person_id
                        and s.recipient_person_id == v.person_id and r.asset_id == s.asset_id
                        and r.person_id == s.maker_person_id for s in r.shares)
            # builder narrowing (delegated recipient needs permission) accepted as conservative
            share = share and (v.delegation is None or v.privacy_permission)
            return content_read(v, r, origin) or share
        if action == "delete":
            return is_self(v, r)
        if action == "reuse":
            return is_self(v, r) and content_read(v, r, origin)
        return False
    if k == "chat":
        if action == "read":
            return content_read(v, r, origin)
        if action in ("delete", "reuse"):
            if not is_self(v, r):
                return False
            if r.app_id:
                return bool(v.app_id) and r.app_id == v.app_id
            return bool(v.key_hash) and r.owner_key_hash == v.key_hash
        return False
    if k == "request_history":
        if action == "read":
            return content_read(v, r, origin)
        if action in ("delete", "reuse"):
            return is_self(v, r) and (v.delegation is None or v.privacy_permission)
        return False
    if k == "debug_capture":
        if action in ("read", "delete"):
            return is_self(v, r) and (v.delegation is None or v.privacy_permission)
        return False
    raise AssertionError(k)
