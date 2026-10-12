"""A1111 /sdapi/v1 subset (gateway/a1111_api.py): the routes SillyTavern calls, over a fake core."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import a1111_api
from tests.support.access import identity
from gateway.a1111_api import A1111Deps

IDENTITY = identity()  # an App whose plans allow everything
PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()


def _gen(short, aliases, props, *, sync=True):
    fields = {"prompt": {"in": "body", "field": "prompt"},
              "width": {"in": "body", "field": "size", "combine": ["width", "height"]},
              "height": {"in": "body", "field": "size", "combine": ["width", "height"]}}
    for name in props:
        fields.setdefault(name, {"in": "extension", "field": name})
    return {
        "id": f"image.generate.{short}", "short_id": short, "intent": "image.generate", "aliases": aliases, "description": "",
        "inputs": {"type": "object", "properties": props, "required": ["prompt"]},
        "route_inputs": [{"name": "n", "type": "integer", "minimum": 1, "maximum": 10, "in": "body", "field": "n"}],
        "execution": {"recommended": "sync",
                      "sync": {"method": "POST", "path": "/v1/images/generations", "content_type": "application/json",
                               "model": short, "fields": fields} if sync else None, "async": None},
    }


KREA = _gen("krea", ["image"], {
    "prompt": {"type": "string"}, "width": {"type": "integer", "minimum": 64, "maximum": 2048},
    "height": {"type": "integer", "minimum": 64, "maximum": 2048}, "steps": {"type": "integer", "minimum": 1, "maximum": 20},
    "seed": {"type": "integer", "minimum": 0}})
KLEIN = _gen("klein", ["image-fast"], {
    "prompt": {"type": "string"}, "width": {"type": "integer", "minimum": 64, "maximum": 2048},
    "height": {"type": "integer", "minimum": 64, "maximum": 2048}, "steps": {"type": "integer", "minimum": 1, "maximum": 100},
    "guidance": {"type": "number", "minimum": 0, "maximum": 20}, "negative_prompt": {"type": "string"},
    "sampler": {"type": "string", "enum": ["euler", "euler_ancestral"]}, "scheduler": {"type": "string", "enum": ["karras", "normal"]}})
TILE = _gen("tile", [], {"prompt": {"type": "string"}}, sync=False)
EDIT = {**_gen("edit", ["image"], {"prompt": {"type": "string"}}), "intent": "image.edit"}
ALIASES = {"operations": {"images": {"stable": [{"alias": "image"}, {"alias": "image-fast"}]}}}


class Core:
    def __init__(self):
        self.calls = []
        self.reply = lambda kw: httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})

    async def call(self, request, method, path, **kwargs):
        self.calls.append((method, path, kwargs, request.headers.get("authorization")))
        return self.reply(kwargs)


@pytest.fixture()
def env():
    core = Core()

    def authenticate(request, _path):
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": {"message": "Invalid API key."}}, status_code=401)
        return SimpleNamespace(owner="k1", identity=IDENTITY)

    deps = A1111Deps(authenticate=authenticate, call=core.call, catalog=lambda: [KREA, KLEIN, TILE, EDIT],
                     aliases=lambda: ALIASES, owner_of=lambda auth: auth.owner)
    app = FastAPI()
    a1111_api.register_a1111_routes(app, deps)
    return SimpleNamespace(core=core, client=TestClient(app))


H = {"authorization": "Bearer good"}
ST_PAYLOAD = {  # what SillyTavern's generateAutoImage sends, plus its url and auth
    "url": "http://gw:5000", "auth": "u:opk", "prompt": "a fish", "negative_prompt": "bad quality", "sampler_name": "DDIM",
    "scheduler": "normal", "steps": 20, "cfg_scale": 7, "width": 512, "height": 768, "restore_faces": False, "enable_hr": False,
    "hr_upscaler": "Latent", "hr_scale": 2, "hr_additional_modules": [], "denoising_strength": 0.7, "hr_second_pass_steps": 0,
    "override_settings": {"CLIP_stop_at_last_layers": 1}, "override_settings_restore_afterwards": True, "clip_skip": 1,
    "save_images": True, "send_images": True, "do_not_save_grid": False, "do_not_save_samples": False,
}


# --------------------------------------------------------------------- auth


def test_every_route_needs_a_key(env):
    for method, path in [("get", "options"), ("get", "sd-models"), ("post", "txt2img"), ("post", "interrupt")]:
        assert getattr(env.client, method)(f"/sdapi/v1/{path}").status_code == 401, path


def test_basic_auth_from_sillytavern_carries_the_key_as_the_password(env):
    basic = "Basic " + base64.b64encode(b"user:good").decode()
    assert env.client.get("/sdapi/v1/options", headers={"authorization": basic}).status_code == 200
    bare = "Basic " + base64.b64encode(b"good").decode()
    assert env.client.get("/sdapi/v1/options", headers={"authorization": bare}).status_code == 200
    wrong = "Basic " + base64.b64encode(b"user:bad").decode()
    assert env.client.get("/sdapi/v1/options", headers={"authorization": wrong}).status_code == 401


def test_the_core_call_is_made_with_the_callers_key_as_bearer(env):
    basic = "Basic " + base64.b64encode(b"user:good").decode()
    env.client.post("/sdapi/v1/txt2img", json={"prompt": "x"}, headers={"authorization": basic})
    assert env.core.calls[0][3] == "Bearer good"


# ------------------------------------------------ the routes SillyTavern pings


def test_options_reports_model_and_is_not_forge(env):
    body = env.client.get("/sdapi/v1/options", headers=H).json()
    assert body["sd_model_checkpoint"] == "image" and "forge_preset" not in body


def test_sd_models_lists_aliases_first_then_sync_image_workflows_only(env):
    models = env.client.get("/sdapi/v1/sd-models", headers=H).json()
    assert [m["title"] for m in models] == ["image", "image-fast", "krea", "klein"]
    assert all({"title", "model_name", "hash", "filename"} <= set(m) for m in models)


@pytest.mark.parametrize("gate_configured", [False, True])
def test_sd_models_reads_one_catalog_snapshot_for_large_catalog(tmp_path, monkeypatch, gate_configured):
    from gateway import category_enforcement

    entries = [_gen(f"model-{i}", [], {"prompt": {"type": "string"}}) for i in range(100)]
    catalog_calls = []

    def catalog():
        catalog_calls.append(True)
        return entries

    gate = category_enforcement.CategoryGate(tmp_path / "categories.db") if gate_configured else None
    monkeypatch.setattr(category_enforcement, "_GATE", gate)
    deps = A1111Deps(authenticate=lambda r, p: SimpleNamespace(owner="k1", identity=IDENTITY), call=Core().call,
                     catalog=catalog, aliases=lambda: {}, owner_of=lambda auth: auth.owner)
    app = FastAPI()
    a1111_api.register_a1111_routes(app, deps)
    response = TestClient(app).get("/sdapi/v1/sd-models")
    assert response.status_code == 200, response.text
    assert [row["title"] for row in response.json()] == [entry["short_id"] for entry in entries]
    assert len(catalog_calls) == 1


def test_sd_models_preserves_title_order_and_first_entry_resolution(monkeypatch):
    from gateway import category_enforcement

    entries = [_gen("primary", ["shared", "other"], {"prompt": {"type": "string"}}),
               _gen("shared", ["primary", "late", "image.generate.primary"], {"prompt": {"type": "string"}})]
    resolved = []

    def visible(identity, request):
        resolved.append((request.model, request.workflow))
        return request.workflow == "image.generate.shared"

    monkeypatch.setattr(a1111_api, "discovery_allows", visible)
    deps = A1111Deps(authenticate=lambda r, p: SimpleNamespace(owner="k1", identity=IDENTITY), call=Core().call,
                     catalog=lambda: entries,
                     aliases=lambda: {"images": {"stable": [{"alias": "shared"}, {"alias": "late"}]}},
                     owner_of=lambda auth: auth.owner)
    app = FastAPI()
    a1111_api.register_a1111_routes(app, deps)
    response = TestClient(app).get("/sdapi/v1/sd-models")
    assert response.status_code == 200, response.text
    assert resolved == [("shared", "image.generate.primary"), ("late", "image.generate.shared"),
                        ("other", "image.generate.primary"), ("primary", "image.generate.primary"),
                        ("image.generate.primary", "image.generate.primary")]
    assert [row["title"] for row in response.json()] == ["late"]


def test_sd_models_does_not_refresh_catalog_between_titles(monkeypatch):
    from gateway import category_enforcement

    monkeypatch.setattr(category_enforcement, "_GATE", None)
    catalog_calls = []

    def catalog():
        catalog_calls.append(True)
        return [KREA, KLEIN] if len(catalog_calls) == 1 else []

    deps = A1111Deps(authenticate=lambda r, p: SimpleNamespace(owner="k1", identity=IDENTITY), call=Core().call,
                     catalog=catalog, aliases=lambda: ALIASES, owner_of=lambda auth: auth.owner)
    app = FastAPI()
    a1111_api.register_a1111_routes(app, deps)
    response = TestClient(app).get("/sdapi/v1/sd-models")
    assert response.status_code == 200, response.text
    assert [row["title"] for row in response.json()] == ["image", "image-fast", "krea", "klein"]
    assert len(catalog_calls) == 1


def test_set_model_is_remembered_per_key_and_validated(env):
    assert env.client.post("/sdapi/v1/options", json={"sd_model_checkpoint": "klein"}, headers=H).status_code == 200
    assert env.client.get("/sdapi/v1/options", headers=H).json()["sd_model_checkpoint"] == "klein"
    bad = env.client.post("/sdapi/v1/options", json={"sd_model_checkpoint": "sd15"}, headers=H)
    assert bad.status_code == 422 and "unknown model" in bad.json()["detail"][0]["msg"]
    other = env.client.post("/sdapi/v1/options", json={"samples_format": "jpg"}, headers=H)
    assert other.status_code == 422 and "cannot be changed" in other.json()["detail"][0]["msg"]


def test_samplers_schedulers_upscalers_and_vae_have_the_shapes_sillytavern_reads(env):
    samplers = env.client.get("/sdapi/v1/samplers", headers=H).json()
    assert [s["name"] for s in samplers] == ["Default", "Euler", "Euler a"]
    schedulers = env.client.get("/sdapi/v1/schedulers", headers=H).json()
    assert [s["name"] for s in schedulers] == ["automatic", "karras", "normal"]
    assert env.client.get("/sdapi/v1/upscalers", headers=H).json()[0]["name"] == "None"
    assert env.client.get("/sdapi/v1/latent-upscale-modes", headers=H).json() == []
    assert env.client.get("/sdapi/v1/sd-vae", headers=H).json() == []


def test_progress_is_idle_when_nothing_runs(env):
    body = env.client.get("/sdapi/v1/progress", headers=H).json()
    assert body["progress"] == 0.0 and body["state"]["job_count"] == 0


# ------------------------------------------------------------------ txt2img


def test_txt2img_returns_base64_images_and_an_info_string(env):
    resp = env.client.post("/sdapi/v1/txt2img", json=ST_PAYLOAD, headers=H)
    assert resp.status_code == 200
    body = resp.json()
    assert body["images"] == [PNG_B64]
    info = json.loads(body["info"])
    assert info["prompt"] == "a fish" and info["width"] == 512 and info["sd_model_name"] == "krea"
    assert "url" not in body["parameters"] and "auth" not in body["parameters"]


def test_txt2img_builds_the_core_image_request_from_the_manifest_fields(env):
    env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "seed": 7}, headers=H)
    method, path, kwargs, _ = env.core.calls[0]
    assert (method, path) == ("POST", "/v1/images/generations")
    assert kwargs["json"] == {
        "model": "krea", "prompt": "a fish", "size": "512x768", "response_format": "b64_json",
        "extensions": {"com.azizam.workflow": {"steps": 20, "seed": 7}},
    }


def test_txt2img_reports_the_seed_it_used_when_none_was_given(env):
    body = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "seed": -1}, headers=H).json()
    sent = env.core.calls[0][2]["json"]["extensions"]["com.azizam.workflow"]["seed"]
    assert json.loads(body["info"])["seed"] == sent and sent >= 0


def test_ignored_knobs_are_named_in_a_header_not_dropped_silently(env):
    resp = env.client.post("/sdapi/v1/txt2img", json=ST_PAYLOAD, headers=H)
    ignored = resp.headers["x-azizam-ignored-params"].split(",")
    assert {"negative_prompt", "cfg_scale", "sampler_name", "scheduler"} <= set(ignored)


def test_a_model_that_uses_the_knobs_receives_them(env):
    env.client.post("/sdapi/v1/options", json={"sd_model_checkpoint": "image-fast"}, headers=H)
    resp = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "sampler_name": "Euler a", "scheduler": "karras"}, headers=H)
    ext = env.core.calls[0][2]["json"]["extensions"]["com.azizam.workflow"]
    assert ext["sampler"] == "euler_ancestral" and ext["scheduler"] == "karras" and ext["guidance"] == 7.0
    assert ext["negative_prompt"] == "bad quality"
    assert "x-azizam-ignored-params" not in resp.headers


def test_steps_over_the_model_limit_are_clamped_and_say_so(env):
    resp = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "steps": 50}, headers=H)
    assert env.core.calls[0][2]["json"]["extensions"]["com.azizam.workflow"]["steps"] == 20
    assert "steps(clamped to 20)" in resp.headers["x-azizam-ignored-params"]


@pytest.mark.parametrize("field,value,needle", [
    ("enable_hr", True, "hires fix"),
    ("restore_faces", True, "face restoration"),
    ("alwayson_scripts", {"ADetailer": {"args": [True]}}, "ADetailer"),
    ("styles", ["anime"], "styles"),
    ("subseed_strength", 0.3, "variation seeds"),
    ("prompt", "a cat <lora:cute:0.8>", "LoRA"),
    ("override_settings", {"eta_noise_seed_delta": 31337}, "not supported"),
])
def test_output_changing_fields_are_rejected_before_any_core_call(env, field, value, needle):
    resp = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, field: value}, headers=H)
    assert resp.status_code == 422 and needle in resp.json()["detail"][0]["msg"]
    assert env.core.calls == []


def test_unknown_sd_vae_is_ignored_and_named_like_real_a1111(env):
    # SillyTavern sends a stale sd_vae; A1111 warns "Couldn't find VAE named ..." and continues.
    payload = {**ST_PAYLOAD, "override_settings": {"sd_vae": "sdxl.vae.safetensors"}}
    resp = env.client.post("/sdapi/v1/txt2img", json=payload, headers=H)
    assert resp.status_code == 200
    assert "override_settings.sd_vae" in resp.headers["x-azizam-ignored-params"].split(",")
    for automatic in ("Automatic", "None", ""):
        resp = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "override_settings": {"sd_vae": automatic}}, headers=H)
        assert resp.status_code == 200 and "sd_vae" not in resp.headers.get("x-azizam-ignored-params", "")


def test_size_outside_the_model_range_is_a_422_not_a_silent_resize(env):
    resp = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "width": 8192}, headers=H)
    assert resp.status_code == 422 and resp.json()["detail"][0]["loc"] == ["body", "width"]


def test_batch_size_times_n_iter_becomes_n(env):
    env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "batch_size": 2, "n_iter": 2}, headers=H)
    assert env.core.calls[0][2]["json"]["n"] == 4
    assert env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "batch_size": 11}, headers=H).status_code == 422


def test_send_images_false_returns_no_images(env):
    assert env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "send_images": False}, headers=H).json()["images"] == []


def test_a_field_of_the_wrong_type_is_a_422_not_a_500(env):
    resp = env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "steps": "many"}, headers=H)
    assert resp.status_code == 422 and env.core.calls == []


def test_prompt_is_required(env):
    assert env.client.post("/sdapi/v1/txt2img", json={"steps": 5}, headers=H).status_code == 422


def test_core_errors_keep_their_status_in_an_a1111_detail_body(env):
    env.core.reply = lambda kw: httpx.Response(403, json={"error": {"message": "profile may not generate images", "code": "policy_denied"}})
    resp = env.client.post("/sdapi/v1/txt2img", json=ST_PAYLOAD, headers=H)
    assert resp.status_code == 403 and resp.json() == {"detail": "profile may not generate images"}


def test_a_core_reply_without_inline_images_is_a_502(env):
    env.core.reply = lambda kw: httpx.Response(200, json={"data": [{"url": "https://x/y.png"}]})
    assert env.client.post("/sdapi/v1/txt2img", json=ST_PAYLOAD, headers=H).status_code == 502


def test_model_field_and_override_pick_the_model_for_one_request(env):
    env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "model": "klein"}, headers=H)
    assert env.core.calls[0][2]["json"]["model"] == "klein"
    env.client.post("/sdapi/v1/txt2img", json={**ST_PAYLOAD, "override_settings": {"sd_model_checkpoint": "krea"}}, headers=H)
    assert env.core.calls[1][2]["json"]["model"] == "krea"


# --------------------------------------------------------- unsupported routes


@pytest.mark.parametrize("name", ["img2img", "extra-single-image", "interrogate", "png-info"])
def test_known_a1111_routes_we_do_not_serve_are_501_with_a_reason(env, name):
    resp = env.client.post(f"/sdapi/v1/{name}", json={}, headers=H)
    assert resp.status_code == 501 and "not supported" in resp.json()["detail"]


def test_interrupt_with_nothing_running_is_harmless(env):
    assert env.client.post("/sdapi/v1/interrupt", headers=H).status_code == 200


def test_interrupt_cancels_the_running_request_and_answers_like_a1111():
    import asyncio

    started = asyncio.Event()
    cancelled = []

    async def slow_call(request, method, path, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    deps = A1111Deps(authenticate=lambda r, p: SimpleNamespace(owner="k1", identity=IDENTITY), call=slow_call, catalog=lambda: [KREA],
                     aliases=lambda: ALIASES, owner_of=lambda auth: auth.owner)
    app = FastAPI()
    a1111_api.register_a1111_routes(app, deps)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            pending = asyncio.create_task(http.post("/sdapi/v1/txt2img", json={"prompt": "x"}))
            await started.wait()
            progress = (await http.get("/sdapi/v1/progress")).json()
            assert progress["state"]["job_count"] == 1
            await http.post("/sdapi/v1/interrupt")
            return await pending

    resp = asyncio.run(scenario())
    assert cancelled == [True]
    assert resp.status_code == 200 and resp.json()["images"] == []
    assert json.loads(resp.json()["info"]) == {"interrupted": True}


# ------------------------------------------------------- owner isolation

def _interrupt_between_callers(owner_a, owner_b, *, deny_b=False):
    """Exercise real routes over fake auth/core only; no proxy or live workload."""
    import asyncio

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls, cancelled = [], []
        owners = {"Bearer caller-a": owner_a, "Bearer caller-b": owner_b}
        # Policy is deliberately faked at the core boundary, as in env above.
        # This tests the router's task cleanup, not production plan enforcement.
        plans = {"Bearer caller-a": {"image.generate"},
                 "Bearer caller-b": set() if deny_b else {"image.generate"}}

        def authenticate(request, _path):
            key = request.headers.get("authorization")
            if key not in owners:
                return JSONResponse({"error": {"message": "Invalid API key."}}, status_code=401)
            # Like the proxy's AuthContext: an identity carrying the credential hash.
            return SimpleNamespace(owner=owners[key], identity=SimpleNamespace(key_hash=f"hash-{key[-1]}"))

        async def slow_call(request, method, path, **kwargs):
            key = request.headers["authorization"]
            calls.append(key)
            if "image.generate" not in plans[key]:
                return httpx.Response(403, json={"error": {
                    "message": "profile may not generate images", "code": "policy_denied"}})
            assert key == "Bearer caller-a"
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(key)
                raise
            return httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})

        deps = A1111Deps(authenticate=authenticate, call=slow_call, catalog=lambda: [KREA],
                         aliases=lambda: ALIASES, owner_of=lambda auth: auth.owner)
        app = FastAPI()
        a1111_api.register_a1111_routes(app, deps)
        headers_a = {"authorization": "Bearer caller-a"}
        headers_b = {"authorization": "Bearer caller-b"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            async def progress(headers):
                response = await http.get("/sdapi/v1/progress", headers=headers)
                assert response.status_code == 200
                return response.json()["state"]

            pending = asyncio.create_task(http.post(
                "/sdapi/v1/txt2img", json={"prompt": "x"}, headers=headers_a))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                assert not pending.done()
                assert (await progress(headers_a))["job_count"] == 1
                if deny_b:
                    denied = await http.post(
                        "/sdapi/v1/txt2img", json={"prompt": "x"}, headers=headers_b)
                    assert denied.status_code == 403
                    assert denied.json() == {"detail": "profile may not generate images"}
                    assert calls == ["Bearer caller-a", "Bearer caller-b"]
                    assert not pending.done() and cancelled == []

                before_b = await progress(headers_b)
                interrupt = await http.post("/sdapi/v1/interrupt", headers=headers_b)
                assert interrupt.status_code == 200
                # Let cancellation reach the blocked fake before releasing it.
                await asyncio.sleep(0)
                was_cancelled = cancelled == ["Bearer caller-a"]
                after_interrupt_a = await progress(headers_a)
                if not was_cancelled:
                    assert not pending.done()
                    assert after_interrupt_a["job_count"] == 1
                release.set()
                response = await asyncio.wait_for(asyncio.shield(pending), timeout=5)
                assert response.status_code == 200
                body = response.json()
                if was_cancelled:
                    assert body["images"] == []
                    assert json.loads(body["info"]) == {"interrupted": True}
                else:
                    assert cancelled == []
                    assert body["images"] == [PNG_B64]
                    assert not json.loads(body["info"]).get("interrupted", False)
                after_a, after_b = await progress(headers_a), await progress(headers_b)
                assert after_a["job_count"] == after_b["job_count"] == 0
                assert calls == (["Bearer caller-a", "Bearer caller-b"] if deny_b
                                 else ["Bearer caller-a"])
                return SimpleNamespace(before_b=before_b, after_a=after_a, after_b=after_b,
                                       cancelled=was_cancelled)
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    return asyncio.run(scenario())


def test_different_owners_cannot_interrupt_or_see_each_others_jobs():
    result = _interrupt_between_callers("user:a", "user:b")
    assert result.before_b["job_count"] == 0
    assert not result.cancelled
    assert not result.after_a["interrupted"] and not result.after_b["interrupted"]


def test_grant_nothing_caller_has_no_tasks_and_interrupt_affects_nobody_else():
    result = _interrupt_between_callers("user:a", "user:denied", deny_b=True)
    assert result.before_b["job_count"] == 0
    assert not result.cancelled
    assert not result.after_a["interrupted"] and not result.after_b["interrupted"]


def test_shared_app_owner_currently_exposes_jobs_and_allows_cross_caller_interrupt():
    result = _interrupt_between_callers("app:shared", "app:shared")
    assert result.before_b["job_count"] == 1
    assert result.cancelled
    assert result.after_a["interrupted"] and result.after_b["interrupted"]


class _AnonymousCallerIsolationError(AssertionError):
    """Only verified anonymous cross-caller isolation violations may xfail."""


def test_none_owner_callers_cannot_see_or_interrupt_each_others_generation():
    result = _interrupt_between_callers(None, None)
    # Fixed: unattributed callers get per-request state, never a shared one.
    if result.before_b["job_count"] != 0 or result.cancelled:
        raise _AnonymousCallerIsolationError(
            f"anonymous cross-caller leak: B job_count={result.before_b['job_count']}; "
            f"A actually cancelled by B={result.cancelled}")
