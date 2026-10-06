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
from gateway.a1111_api import A1111Deps

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
        return SimpleNamespace(owner="k1")

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

    deps = A1111Deps(authenticate=lambda r, p: SimpleNamespace(owner="k1"), call=slow_call, catalog=lambda: [KREA],
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
