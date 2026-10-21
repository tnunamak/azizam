"""Independent round-2 reviewer oracle, TD §2 / OV §3.1 at c6968dc8.

Predicates copied verbatim from td_oracle_r2.py; no production imports.
strict_share=False applies D52: a delegated recipient needs privacy permission
for an included prompt. True is the reviewer's alternative reading, not policy.
Original probe SHA-256: ac738a5b6a550962a9ce12eef2de9d54b1b308a306a3f2ee5020e180e8a74d88 .
"""

nn = lambda x: x not in (None, "")

def tdself(v, r):  # Self: both non-null, equal
    return nn(v.person_id) and nn(r.person_id) and v.person_id == r.person_id

def own_output(v, r):
    o = r.origin
    if o is None:      # legacy = browser-created, never OwnOutput
        return False
    if v.auth_kind == "app_key":   # attributed row created by its same non-null App (credential id recorded)
        return o.auth_kind == "app_key" and nn(o.credential_id) and nn(v.app_id) and r.app_id == v.app_id
    if v.auth_kind == "mcp_grant":  # exact same non-null connection_id
        return o.auth_kind == "mcp_grant" and nn(v.connection_id) and o.connection_id == v.connection_id
    return False

def content_read(v, r):
    if r.person_id is None:
        return (v.person_id is None and v.auth_kind in ("app_key", "service_key", "trusted_address")
                and nn(v.app_id) and r.app_id == v.app_id)
    return tdself(v, r) and (v.auth_kind == "session" or own_output(v, r) or v.privacy_permission)

def delegated_ok(v):
    return v.auth_kind == "session" or v.privacy_permission

def share_hit(v, r, prompt):
    for s in r.shares:
        if not (s.active and nn(s.share_id) and nn(s.maker_person_id) and nn(v.person_id) and s.recipient_person_id == v.person_id):
            continue
        if prompt:
            if s.include_prompt and r.asset_id == s.asset_id and r.person_id == s.maker_person_id:
                return True
        elif r.asset_id in (s.asset_id, s.poster_asset_id):
            return True
    return False

def expected(v, r, a, strict_share):
    if not r.available: return "deny"
    k = r.kind
    if k in ("creation", "voice", "receipt", "job"):
        own = bool({x for x in v.owner_keys if x} & {x for x in r.owner_keys if x})
        allowed = {"creation": {"read", "delete", "reuse", "link"}, "voice": {"read", "delete", "reuse"},
                   "receipt": {"read"}, "job": {"read", "cancel"}}[k]
        if own and a in allowed: return "own"
        if k == "creation" and a == "share" and own and nn(v.person_id) and tuple(r.maker_person_ids) == (v.person_id,): return "own"
        if k == "creation" and a in ("read", "link") and share_hit(v, r, False): return "shared"
        return "deny"
    if k == "prompt":
        if a == "read":
            if content_read(v, r): return "own"
            if share_hit(v, r, True) and (strict_share or delegated_ok(v)): return "shared"
            return "deny"
        if a == "delete": return "own" if tdself(v, r) else "deny"
        if a == "reuse": return "own" if tdself(v, r) and content_read(v, r) else "deny"
        return "deny"
    if k == "chat":
        if a == "read": return "own" if content_read(v, r) else "deny"
        if a in ("delete", "reuse"):
            if not tdself(v, r): return "deny"
            if nn(r.app_id): return "own" if v.app_id == r.app_id else "deny"
            return "own" if nn(v.key_hash) and v.key_hash == r.owner_key_hash else "deny"
        return "deny"
    if k == "request_history":
        if a == "read": return "own" if content_read(v, r) else "deny"
        if a in ("delete", "reuse"): return "own" if tdself(v, r) and delegated_ok(v) else "deny"
        return "deny"
    if k == "debug_capture":
        return "own" if a in ("read", "delete") and tdself(v, r) and delegated_ok(v) else "deny"
    raise AssertionError(k)
