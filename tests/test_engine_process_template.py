"""A definition's process_template: slot values become the literal process block ProcessDriver runs.

The resolver is pure, so most cases call it with folders in a temporary directory. The route cases mount the real
engine routes and the real EngineService on a real admin store, and the last case saves a record from a test-only
template definition, starts it under the real ProcessDriver around a stub server and stops it with the owner
lifecycle route.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, engine_admin, lifecycle_drivers, process_template, service_registry
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from gateway.engine_definitions import (
    DEFINITIONS_PATH,
    SCHEMA_PATH,
    EngineDefinitionError,
    parse_engine_catalog,
)
from gateway.engine_lifecycle import EngineLifecycle
from gateway.engine_process import ProcessDriver
from gateway.process_template import NO_FOLDERS_WHY, TemplateRefused, resolve
from gateway.swap_drain import SwapDrain
from tests.test_engine_contract_runtime import free_port
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

SHIPPED = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

TEMPLATE = {
    "argv": ["{folder}/venv/bin/python", "{folder}/stub.py", "--port", "{port}"],
    "cwd": "{folder}",
    "env": ["STUB_TOKEN"],
    "slots": {
        "folder": {"type": "path", "kind": "dir", "locate": ["stub.py"]},
        "port": {"type": "port"},
    },
    "ready": {"http": {"path": "/health", "expect": 200}},
    "stop": {"timeout_s": 2},
}

STUB = (
    "import http.server, sys\n"
    "class Handler(http.server.BaseHTTPRequestHandler):\n"
    " def do_GET(self):\n"
    "  self.send_response(200); self.end_headers(); self.wfile.write(b'{}')\n"
    " def log_message(self, *args): pass\n"
    "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
    "http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
)


def template_with(**changes):
    out = copy.deepcopy(TEMPLATE)
    out.update(changes)
    return out


def catalog_with(template):
    """The shipped definitions plus one test-only definition that carries ``template``."""
    data = copy.deepcopy(SHIPPED)
    definition = copy.deepcopy(next(d for d in data["definitions"] if d["definition"] == "openai-compatible"))
    definition["definition"] = "template-test"
    definition["title"] = "Template test"
    definition["process_template"] = copy.deepcopy(template)
    data["definitions"].append(definition)
    return parse_engine_catalog(data, schema=SCHEMA, source="test.json")


class Folders:
    """An allowed folder holding one engine install, and a second folder that is not allowed."""

    def __init__(self, tmp_path: Path) -> None:
        self.allowed_root = tmp_path / "allowed"
        self.install = self.allowed_root / "app"
        (self.install / "venv" / "bin").mkdir(parents=True)
        (self.install / "stub.py").write_text(STUB)
        os.symlink(sys.executable, self.install / "venv" / "bin" / "python")
        self.outside = tmp_path / "outside"
        (self.outside / "venv" / "bin").mkdir(parents=True)
        (self.outside / "stub.py").write_text(STUB)
        os.symlink(sys.executable, self.outside / "venv" / "bin" / "python")
        self.allowed = [str(self.allowed_root)]

    def values(self, port=40111, folder=None):
        return {"folder": str(folder or self.install), "port": port}


@pytest.fixture
def folders(tmp_path):
    return Folders(tmp_path)


def refusal(template, values, **kw):
    kw.setdefault("port_free", lambda _port: True)
    with pytest.raises(TemplateRefused) as caught:
        resolve(template, values, **kw)
    return caught.value


# -- the resolver --------------------------------------------------------------------------------


def test_a_template_resolves_to_a_literal_process_block(folders):
    resolved = resolve(TEMPLATE, folders.values(), allowed_folders=folders.allowed, port_free=lambda _port: True)
    real = os.path.realpath(folders.install)
    assert resolved.argv == [f"{real}/venv/bin/python", f"{real}/stub.py", "--port", "40111"]
    block = resolved.process_block()
    assert block == {"driver": "process", "start": resolved.argv, "port": {"fixed": 40111}, "stop_timeout_s": 2.0,
                     "cwd": real}
    assert resolved.observations() == {"process_ready": {"http": {"path": "/health", "expect": 200}}}
    assert resolved.address == "http://127.0.0.1:40111"
    # Nothing of the template is left: no slot token, and no environment value of any kind.
    assert not any("{" in word for word in [*block["start"], block["cwd"]]) and "env" not in block
    assert resolved.preview()["env_names"] == ["STUB_TOKEN"]


def test_an_empty_allowed_folder_list_refuses_every_path_slot(folders):
    refused = refusal(TEMPLATE, folders.values(), allowed_folders=[])
    assert [(p.code, p.slot, p.why) for p in refused.problems] == [("no_folders", "folder", NO_FOLDERS_WHY)]
    assert str(refused) == "Add this folder to the folders Azizam may run engines from first."


def test_a_path_outside_the_allowed_folders_is_refused(folders):
    refused = refusal(TEMPLATE, folders.values(folder=folders.outside), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "outside_allowed_folders" and refused.problems[0].slot == "folder"


def test_a_symlink_that_escapes_the_allowed_folders_is_refused(folders):
    link = folders.allowed_root / "link"
    os.symlink(folders.outside, link)
    refused = refusal(TEMPLATE, folders.values(folder=link), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "outside_allowed_folders"


def test_a_folder_missing_a_locate_marker_is_refused(folders):
    (folders.install / "stub.py").unlink()
    refused = refusal(TEMPLATE, folders.values(), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "marker_missing" and "stub.py" in refused.problems[0].why


def test_a_marker_that_is_a_symlink_out_of_the_allowed_folders_is_refused(folders):
    (folders.install / "stub.py").unlink()
    os.symlink(folders.outside / "stub.py", folders.install / "stub.py")
    refused = refusal(TEMPLATE, folders.values(), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "marker_missing"


@pytest.mark.parametrize("value, code", [
    ("relative/app", "not_absolute"),
    ("/does/not/exist/anywhere", "missing"),
    (12, "not_a_path"),
])
def test_a_path_value_must_be_an_absolute_existing_path(folders, value, code):
    refused = refusal(TEMPLATE, {"folder": value, "port": 40111}, allowed_folders=folders.allowed)
    assert refused.problems[0].code == code


def test_a_path_of_the_wrong_kind_is_refused(folders):
    refused = refusal(TEMPLATE, folders.values(folder=folders.install / "stub.py"), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "wrong_kind"


def test_an_unknown_token_is_refused(folders):
    template = template_with(argv=["{folder}/venv/bin/python", "{folder}/stub.py", "{nope}"])
    refused = refusal(template, folders.values(), allowed_folders=folders.allowed)
    assert [(p.code, p.slot) for p in refused.problems] == [("unknown_token", "nope")]


def test_a_value_for_an_unknown_slot_is_refused(folders):
    refused = refusal(TEMPLATE, {**folders.values(), "extra": "x"}, allowed_folders=folders.allowed)
    assert [(p.code, p.slot) for p in refused.problems] == [("unknown_slot", "extra")]


def test_a_missing_slot_value_is_refused(folders):
    refused = refusal(TEMPLATE, {"folder": str(folders.install)}, allowed_folders=folders.allowed)
    assert [(p.code, p.slot) for p in refused.problems] == [("missing_slot", "port")]
    assert [p.code for p in refusal(TEMPLATE, {}, allowed_folders=folders.allowed).problems] == ["missing_slot", "missing_slot"]


def test_a_busy_port_is_refused(folders):
    refused = refusal(TEMPLATE, folders.values(), allowed_folders=folders.allowed, port_free=lambda _port: False)
    assert [(p.code, p.slot) for p in refused.problems] == [("port_busy", "port")]


def test_a_port_the_gateway_listens_on_is_refused(folders):
    refused = refusal(TEMPLATE, folders.values(port=5000), allowed_folders=folders.allowed, reserved_ports={5000, 15000})
    assert [(p.code, p.slot) for p in refused.problems] == [("reserved_port", "port")]


@pytest.mark.parametrize("port", [80, 1023, 65536, "40111", True, 40111.0])
def test_a_port_outside_1024_to_65535_or_not_a_whole_number_is_refused(folders, port):
    refused = refusal(TEMPLATE, folders.values(port=port), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "bad_port"


def test_a_port_bound_by_another_process_is_not_free(folders):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        taken = holder.getsockname()[1]
        assert process_template.port_is_free(taken) is False
        refused = refusal(TEMPLATE, folders.values(port=taken), allowed_folders=folders.allowed,
                          port_free=process_template.port_is_free)
    assert refused.problems[0].code == "port_busy"
    assert process_template.port_is_free(taken) is True


def test_a_shell_as_the_program_is_refused(folders):
    shell = folders.install / "venv" / "bin" / "bash"
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    template = template_with(argv=["{folder}/venv/bin/bash", "-c", "{folder}/stub.py {port}"])
    refused = refusal(template, folders.values(), allowed_folders=folders.allowed)
    assert [p.code for p in refused.problems] == ["shell_program"]
    # A shell reached through a symlink with an innocent name is a shell too.
    os.symlink("/bin/sh", folders.install / "venv" / "bin" / "runner")
    hidden = template_with(argv=["{folder}/venv/bin/runner", "{folder}/stub.py"])
    assert [p.code for p in refusal(hidden, folders.values(), allowed_folders=folders.allowed).problems] == ["shell_program"]


@pytest.mark.parametrize("program", ["/bin/sh", "/usr/bin/env"])
def test_a_program_outside_the_allowed_folders_is_refused(folders, program):
    template = template_with(argv=[program, "{folder}/stub.py", "{port}"])
    refused = refusal(template, folders.values(), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "program_outside_allowed_folders"


def test_a_program_that_does_not_exist_is_refused(folders):
    (folders.install / "venv" / "bin" / "python").unlink()
    refused = refusal(TEMPLATE, folders.values(), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "program_missing"


def test_a_folder_with_a_brace_in_its_path_is_refused(folders):
    odd = folders.allowed_root / "a{port}b"
    odd.mkdir()
    (odd / "stub.py").write_text(STUB)
    refused = refusal(TEMPLATE, folders.values(folder=odd), allowed_folders=folders.allowed)
    assert refused.problems[0].code == "brace_in_path"


# -- the template as data ------------------------------------------------------------------------


def test_the_definitions_schema_takes_a_template_and_rejects_unknown_keys_and_a_long_stop():
    assert catalog_with(TEMPLATE).definition("template-test")["process_template"]["stop"] == {"timeout_s": 2}
    for broken in (template_with(shell=True), template_with(stop={"timeout_s": 31}), template_with(stop={"timeout_s": 2, "kill": 1}),
                   template_with(ready={"http": {"path": "/health", "expect": 200, "extra": 1}}),
                   template_with(slots={"folder": {"type": "path", "kind": "dir", "glob": "*"}, "port": {"type": "port"}}),
                   template_with(env=["TOKEN=secret-value"])):
        with pytest.raises(EngineDefinitionError):
            catalog_with(broken)


def test_the_catalog_refuses_a_template_whose_tokens_do_not_match_its_slots():
    with pytest.raises(EngineDefinitionError, match="names no declared slot"):
        catalog_with(template_with(argv=["{folder}/venv/bin/python", "{missing}"]))
    with pytest.raises(EngineDefinitionError, match="exactly one port slot"):
        catalog_with(template_with(slots={**TEMPLATE["slots"], "second": {"type": "port"}}, argv=[*TEMPLATE["argv"], "{second}"]))
    with pytest.raises(EngineDefinitionError, match="no argv word or cwd uses this slot"):
        catalog_with(template_with(slots={**TEMPLATE["slots"], "idle": {"type": "path", "kind": "dir"}}))
    with pytest.raises(EngineDefinitionError, match="absolute path or start with a path slot"):
        catalog_with(template_with(argv=["python", "{folder}/stub.py", "{port}"]))
    with pytest.raises(EngineDefinitionError, match="inside the folder"):
        catalog_with(template_with(slots={"folder": {"type": "path", "kind": "dir", "locate": ["../x"]}, "port": {"type": "port"}}))


def test_no_shipped_definition_carries_a_template_yet():
    assert not [d["definition"] for d in SHIPPED["definitions"] if "process_template" in d]


# -- the routes ----------------------------------------------------------------------------------


def sign_in_stand_in(request):
    role = request.headers.get("x-role")
    if role == "owner":
        return AdminContext(subject="owner-1", roles=frozenset({"owner", "admin"}), actor_id="owner-1")
    if role == "admin":
        return AdminContext(subject="admin-1", roles=frozenset({"admin"}), actor_id="admin-1")
    return JSONResponse({"error": {"code": "admin_forbidden"}}, status_code=403)


def answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class Rig:
    """The real engine routes and EngineService over a real admin store with the test-only template definition."""

    def __init__(self, tmp_path: Path, monkeypatch, folders: Folders, *, template=TEMPLATE, allow=True):
        self.db = tmp_path / "admin.db"
        admin_store.init_db(self.db)
        self.folders = folders
        self.store = AdminSQLiteStore(self.db)
        if allow:
            admin_store.put_operator_setting(process_template.SETTING, {"folders": folders.allowed}, path=self.db)
        catalog = catalog_with(template)
        self.service = engine_admin.EngineService(self.store, catalog_loader=lambda: catalog, gateway_ports=lambda: {5000, 15000})
        self.commands_allowed = [True]
        self.driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: self.commands_allowed[0])
        monkeypatch.setattr(lifecycle_drivers, "_process_driver", self.driver)
        self.drain = SwapDrain()
        self.backends: dict = {}
        lifecycle = EngineLifecycle(backends=lambda: self.backends, in_flight=self.drain.in_flight,
                                    commands_allowed=lambda: self.commands_allowed[0], verify_s=3.0)
        router = APIRouter(prefix="/admin/api")
        engine_admin.register_engine_routes(router, service=self.service, guard=sign_in_stand_in,
                                            read_json=lambda request: request.json(), lifecycle=lifecycle)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.owner = AdminContext(subject="owner-1", roles=frozenset({"owner", "admin"}), actor_id="owner-1")
        self.admin = AdminContext(subject="admin-1", roles=frozenset({"admin"}), actor_id="admin-1")

    def preview(self, slots, role="owner", definition="template-test"):
        return self.client.post("/admin/api/engines/process-preview", json={"definition": definition, "slots": slots},
                                headers={"x-role": role})

    def create(self, slots, ctx=None, **body):
        generation = (self.store.get_service_registry_snapshot()[1])
        return asyncio.run(self.service.create(ctx or self.owner, {"definition": "template-test", "name": "stub engine",
                                                                  "slots": slots, **body}, expected_generation=generation))

    def registry(self):
        return self.store.get_service_registry()

    def close(self):
        self.client.close()
        for service in self.backends.values():
            self.driver.stop(service)


@pytest.fixture
def rig(tmp_path, monkeypatch, folders):
    made = Rig(tmp_path, monkeypatch, folders)
    try:
        yield made
    finally:
        made.close()


def test_preview_answers_the_command_and_stores_and_runs_nothing(rig, folders, tmp_path):
    port = free_port()
    before = rig.registry()
    answer = rig.preview(folders.values(port=port))
    assert answer.status_code == 200, answer.text
    real = os.path.realpath(folders.install)
    assert answer.json() == {
        "argv": [f"{real}/venv/bin/python", f"{real}/stub.py", "--port", str(port)], "cwd": real,
        "env_names": ["STUB_TOKEN"], "port": port, "ready": {"http": {"path": "/health", "expect": 200}}, "problems": [],
    }
    assert rig.registry() == before  # nothing stored
    assert not answers(port)  # nothing run
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").iterdir())


def test_preview_lists_every_reason_a_value_is_refused(rig, folders):
    answer = rig.preview({"folder": str(folders.outside), "port": 80})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["argv"] == [] and body["port"] is None
    assert {(p["code"], p["slot"]) for p in body["problems"]} == {("outside_allowed_folders", "folder"), ("bad_port", "port")}
    assert all(p["why"] for p in body["problems"])


def test_preview_with_no_folders_allowed_says_to_add_one_first(tmp_path, monkeypatch, folders):
    made = Rig(tmp_path, monkeypatch, folders, allow=False)
    try:
        problems = made.preview(folders.values()).json()["problems"]
    finally:
        made.close()
    assert [(p["code"], p["why"]) for p in problems] == [("no_folders", NO_FOLDERS_WHY)]


def test_preview_refuses_a_gateway_port_and_a_port_a_saved_engine_uses(rig, folders):
    assert rig.preview(folders.values(port=5000)).json()["problems"][0]["code"] == "reserved_port"
    port = free_port()
    rig.create(folders.values(port=port))
    assert rig.preview(folders.values(port=port)).json()["problems"][0]["code"] == "reserved_port"


def test_preview_is_owner_only(rig, folders):
    for role in ("admin", "member"):
        answer = rig.preview(folders.values(), role=role)
        assert answer.status_code == 403, (role, answer.text)
        assert "argv" not in answer.text


def test_preview_of_a_definition_without_a_template_or_an_unknown_one_is_a_plain_400(rig, folders):
    assert rig.preview(folders.values(), definition="openai-compatible").status_code == 400
    assert rig.preview(folders.values(), definition="nope").status_code == 400


def test_a_record_saved_from_a_template_holds_the_literal_block(rig, folders):
    port = free_port()
    created = rig.create(folders.values(port=port))
    record = rig.registry()["services"][created["service_id"]]
    real = os.path.realpath(folders.install)
    assert record["process"] == {"driver": "process", "start": [f"{real}/venv/bin/python", f"{real}/stub.py", "--port", str(port)],
                                 "port": {"fixed": port}, "stop_timeout_s": 2.0, "cwd": real}
    assert record["observations"]["process_ready"] == {"http": {"path": "/health", "expect": 200}}
    assert record["residency"] == {"idle_owner": "engine"}
    assert record["config"]["address"] == f"http://127.0.0.1:{port}"
    assert not any("{" in word for word in [*record["process"]["start"], record["process"]["cwd"]])
    assert "env" not in record["process"]
    assert "process_template" not in json.dumps(record)  # the template stays in the definition, never in the record
    assert not answers(port)  # saving does not start it


def test_a_saved_record_follows_the_residency_the_template_asks_for(tmp_path, monkeypatch, folders):
    made = Rig(tmp_path, monkeypatch, folders, template=template_with(idle_stop_s=90))
    try:
        created = made.create(folders.values(port=free_port()))
        assert made.registry()["services"][created["service_id"]]["residency"] == {"idle_owner": "gateway", "idle_stop_s": 90.0}
    finally:
        made.close()


def test_saving_refuses_what_the_preview_refuses_and_stores_nothing(rig, folders):
    before = rig.registry()
    for slots, code in ((folders.values(folder=folders.outside), "outside_allowed_folders"),
                        ({"folder": str(folders.install)}, "missing_slot"),
                        (folders.values(port=5000), "reserved_port")):
        with pytest.raises(engine_admin.EngineRouteError) as caught:
            rig.create(slots)
        assert caught.value.status_code == 422 and caught.value.code == "process_template_refused", code
    assert rig.registry() == before


def test_saving_a_started_engine_is_owner_only(rig, folders):
    with pytest.raises(engine_admin.EngineRouteError) as caught:
        rig.create(folders.values(port=free_port()), ctx=rig.admin)
    assert caught.value.status_code == 403
    assert rig.registry()["services"] == {}


def test_saving_does_not_accept_a_different_address_than_the_port_gives(rig, folders):
    with pytest.raises(engine_admin.DraftError):
        rig.create(folders.values(port=free_port()), values={"address": "http://203.0.113.9:9000"})


def test_the_runtime_projection_carries_a_native_process_block_and_its_ready_observation(rig, folders):
    created = rig.create(folders.values(port=free_port()))
    record = rig.registry()["services"][created["service_id"]]
    backend = service_registry.runtime_backend_from_service(record)
    assert backend["process"] == record["process"]
    assert backend["observations"] == {"process_ready": record["observations"]["process_ready"]}
    # A legacy lifecycle's own view of a process driver is not a native block: the projection leaves it to the lifecycle.
    legacy = service_registry._v2_process({"driver": "process", "driver_config": {"start": ["/opt/x/run"]}})
    projected = service_registry.runtime_backend_from_service({**record, "process": legacy, "observations": {"voices": []}})
    assert "process" not in projected and "observations" not in projected


def test_a_saved_template_record_starts_under_the_real_driver_and_the_lifecycle_route_stops_it(rig, folders):
    port = free_port()
    created = rig.create(folders.values(port=port))
    service_id = created["service_id"]
    rig.backends = service_registry.runtime_backends(rig.registry())
    assert service_id in rig.backends
    url = f"/admin/api/engines/{service_id}/lifecycle"

    # With host commands off the record saved fine, and it still may not start.
    rig.commands_allowed[0] = False
    refused = rig.client.post(url, json={"action": "start"}, headers={"x-role": "owner"})
    assert refused.status_code == 409 and refused.json()["result"] == "refused", refused.text
    assert not answers(port)

    rig.commands_allowed[0] = True
    started = rig.client.post(url, json={"action": "start"}, headers={"x-role": "owner"})
    assert started.status_code == 200, started.text
    assert (started.json()["result"], started.json()["state"]) == ("done", "running")
    assert answers(port)
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as reply:  # the template's ready path
        assert reply.status == 200

    stopped = rig.client.post(url, json={"action": "stop"}, headers={"x-role": "owner"})
    assert stopped.status_code == 200, stopped.text
    assert (stopped.json()["result"], stopped.json()["state"]) == ("done", "stopped")
    assert not answers(port)


# -- the allowed folders setting -----------------------------------------------------------------


def test_the_owner_sets_the_allowed_folders_and_nobody_else_can(gateway, tmp_path):
    owner, _, _, admin = owner_and_admin(gateway)
    path = "/admin/api/operator/engine-folders"
    default = owner.get(path)
    assert default.status_code == 200 and default.json() == {"folders": [], "revision": "0"}
    assert admin.get(path).status_code == 403
    folder = tmp_path / "engines"
    folder.mkdir()
    denied = admin.put(path, headers={**csrf_for(admin), "If-Match": "0"}, json={"folders": [str(folder)]})
    assert denied.status_code == 403
    saved = owner.put(path, headers={**csrf_for(owner), "If-Match": "0"}, json={"folders": [str(folder), str(folder) + "/"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["folders"] == [str(folder)]
    assert process_template.allowed_folders(gateway.path) == [str(folder)]
    assert owner.get(path).json()["folders"] == [str(folder)]
    assert admin_store.list_audit(path=gateway.path, action_prefix="operator.engine_folders.")
    stale = owner.put(path, headers={**csrf_for(owner), "If-Match": "0"}, json={"folders": []})
    assert stale.status_code == 409


@pytest.mark.parametrize("folders_value", [["relative/path"], ["/"], ["/does/not/exist/at/all"], [3], "text"])
def test_a_folder_that_cannot_be_allowed_is_refused_and_the_setting_stays(gateway, folders_value):
    owner, *_ = owner_and_admin(gateway)
    path = "/admin/api/operator/engine-folders"
    refused = owner.put(path, headers={**csrf_for(owner), "If-Match": "0"}, json={"folders": folders_value})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid_engine_folders"
    assert process_template.allowed_folders(gateway.path) == []


def test_an_unset_or_unreadable_setting_allows_no_folder(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    assert process_template.allowed_folders(db) == []
    assert process_template.allowed_folders(tmp_path / "missing" / "admin.db") == []


# -- review fixes: the root by another name, untested refusals, step-up and audit ------------------


def test_the_root_folder_named_another_way_or_reached_by_a_symlink_is_refused(gateway, tmp_path):
    owner, *_ = owner_and_admin(gateway)
    link = tmp_path / "rootlink"
    os.symlink("/", link)
    for value in (["//"], ["/./"], [str(link)]):
        refused = owner.put("/admin/api/operator/engine-folders", headers={**csrf_for(owner), "If-Match": "0"},
                            json={"folders": value})
        assert refused.status_code == 400, (value, refused.text)
        assert refused.json()["error"]["code"] == "invalid_engine_folders"
    assert process_template.allowed_folders(gateway.path) == []


def test_a_stored_root_folder_allows_nothing(folders):
    # A value written before this check, or by hand, that names the root is not a root the resolver uses.
    assert process_template._roots(["//", "/", "/./"]) == []
    found = refusal(TEMPLATE, folders.values(), allowed_folders=["//"])
    assert {p.code for p in found.problems} == {"no_folders"}


def test_a_folder_is_stored_as_its_real_path(gateway, tmp_path):
    owner, *_ = owner_and_admin(gateway)
    real = tmp_path / "real-engines"
    real.mkdir()
    link = tmp_path / "engines-link"
    os.symlink(real, link)
    saved = owner.put("/admin/api/operator/engine-folders", headers={**csrf_for(owner), "If-Match": "0"},
                      json={"folders": [str(link)]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["folders"] == [os.path.realpath(real)]
    entry = admin_store.list_audit(path=gateway.path, action_prefix="operator.engine_folders.")[0]
    assert os.path.realpath(real) in json.dumps(entry)


def test_a_folder_change_without_if_match_is_refused(gateway, tmp_path):
    owner, *_ = owner_and_admin(gateway)
    refused = owner.put("/admin/api/operator/engine-folders", headers=csrf_for(owner), json={"folders": [str(tmp_path)]})
    assert refused.status_code == 428, refused.text
    assert process_template.allowed_folders(gateway.path) == []


def test_a_working_folder_that_leaves_the_allowed_folders_is_refused(folders):
    os.symlink(folders.outside, folders.install / "work")
    found = refusal(template_with(cwd="{folder}/work"), folders.values(), allowed_folders=folders.allowed)
    assert [p.code for p in found.problems] == ["cwd_outside_allowed_folders"]


def test_a_file_slot_given_a_folder_is_refused(folders):
    template = template_with(argv=["{folder}/venv/bin/python", "{script}", "--port", "{port}"],
                             slots={**TEMPLATE["slots"], "script": {"type": "path", "kind": "file"}})
    found = refusal(template, {**folders.values(), "script": str(folders.install)}, allowed_folders=folders.allowed)
    assert [(p.code, p.slot) for p in found.problems] == [("wrong_kind", "script")]
    ok = resolve(template, {**folders.values(), "script": str(folders.install / "stub.py")},
                 allowed_folders=folders.allowed, port_free=lambda _port: True)
    assert ok.argv[1] == os.path.realpath(folders.install / "stub.py")


def test_a_program_that_cannot_run_is_refused(folders):
    program = folders.install / "server"
    program.write_text("#!/bin/false\n")
    program.chmod(0o644)
    found = refusal(template_with(argv=["{folder}/server", "--port", "{port}"]), folders.values(),
                    allowed_folders=folders.allowed)
    assert [p.code for p in found.problems] == ["program_missing"]


def test_the_projection_carries_only_the_ready_observation(rig, folders):
    created = rig.create(folders.values(port=free_port()))
    record = rig.registry()["services"][created["service_id"]]
    record = {**record, "observations": {**record["observations"], "voices": ["a"]}}
    backend = service_registry.runtime_backend_from_service(record)
    assert backend["observations"] == {"process_ready": {"http": {"path": "/health", "expect": 200}}}


def test_saving_a_started_engine_from_a_stale_browser_session_needs_a_fresh_sign_in(rig, folders):
    from gateway import step_up

    stale = AdminContext(subject="owner-1", roles=frozenset({"owner", "admin"}), actor_id="owner-1",
                         authenticated_at=1)
    with pytest.raises(step_up.StepUpRequired):
        rig.create(folders.values(port=free_port()), ctx=stale)
    assert not rig.registry().get("services")


def test_the_audit_line_for_a_saved_started_engine_names_its_command_and_port(rig, folders):
    port = free_port()
    rig.create(folders.values(port=port))
    entry = admin_store.list_audit(path=rig.db, action_prefix="operator.service.added")[0]
    text = json.dumps(entry)
    assert str(port) in text and "stub.py" in text
