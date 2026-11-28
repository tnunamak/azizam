"""A fake plan provider for the subscription journey in the browser fixture (web/e2e/subscription-admin.spec.ts).

It serves what the gateway's sign-in and model discovery call, over TLS with a throwaway certificate for 127.0.0.1
(the kind data requires https; serve.py gives only the fixture gateway the certificate to trust):
  GET  /authorize                         a page; the test never signs in here
  GET  /.well-known/openid-configuration  the discovery document
  GET  /jwks                              the public signing key
  POST /oauth/token                       code exchange and refresh. The code is "fake-<nonce>": the nonce the
                                          gateway put in the authorize address comes back in the signed id token
  POST /revoke                            ends the refresh token
  GET  /v1/models                         the model list
  POST /__fixture/models                  {"count": n}: how many models the list holds from now on
It has no state beyond that count and runs only in a fixture.
"""
from __future__ import annotations

import datetime
import functools
import ipaddress
import time
from pathlib import Path

from authlib.jose import JsonWebKey, jwt
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

SCOPE = "openid profile email offline_access plan.use"
EMAIL = "owner@example.test"



@functools.cache
def _key():
    return JsonWebKey.generate_key("RSA", 2048, options={"kid": "fixture-key"}, is_private=True)


_models = {"count": 2}


def write_certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1, valid for a day. Returns (certificate, key) paths."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-plan-upstream")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "plan-upstream-cert.pem", directory / "plan-upstream-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    return cert_path, key_path


app = FastAPI()


def _origin(request: Request) -> str:
    return f"https://{request.headers['host']}"


@app.get("/authorize")
async def authorize() -> HTMLResponse:
    return HTMLResponse("<p>Fixture sign-in page. The browser test never signs in here.</p>")


@app.get("/.well-known/openid-configuration")
async def discovery(request: Request) -> dict:
    origin = _origin(request)
    return {"issuer": origin, "jwks_uri": f"{origin}/jwks", "revocation_endpoint": f"{origin}/revoke"}


@app.get("/jwks")
async def jwks() -> dict:
    return {"keys": [_key().as_dict(is_private=False)]}


def _id_token(request: Request, client_id: str, nonce: str) -> str:
    claims = {"iss": _origin(request), "aud": [client_id], "sub": "acct-fixture", "email": EMAIL,
              "exp": int(time.time()) + 600, "nonce": nonce}
    return jwt.encode({"alg": "RS256", "kid": "fixture-key"}, claims, _key()).decode("ascii")


@app.post("/oauth/token")
async def token(request: Request, grant_type: str = Form(...), client_id: str = Form(""), code: str = Form(""),
                refresh_token: str = Form("")) -> JSONResponse:
    body = {"access_token": f"fixture-access-{time.time_ns()}", "refresh_token": "fixture-refresh",
            "token_type": "Bearer", "expires_in": 3600, "scope": SCOPE}
    if grant_type == "authorization_code":
        if not code.startswith("fake-"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        body["id_token"] = _id_token(request, client_id, code.removeprefix("fake-"))
    elif grant_type != "refresh_token":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    return JSONResponse(body)


@app.post("/revoke")
async def revoke() -> dict:
    return {}


@app.get("/v1/models")
async def models() -> dict:
    return {"models": [{"slug": f"plan-model-{index}", "visibility": "list"} for index in range(1, _models["count"] + 1)]
            + [{"slug": "plan-model-hidden", "visibility": "hide"}]}


@app.post("/__fixture/models")
async def set_models(request: Request) -> dict:
    _models["count"] = int((await request.json())["count"])
    return dict(_models)
