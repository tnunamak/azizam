"""A person's generated avatar: the fixed prompt, the image route it uses, the job, the keep and the remove."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, avatar_generation, avatars, media_fetch, prompt_memory
from gateway.admin_oidc import AdminSession
from gateway.me_avatar import AvatarGeneration, PictureCache, create_me_avatar_router
from gateway.public_failure import PublicFailure

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
# The sign-in picture the fake provider serves: a different image from the one the gateway made.
SIGN_IN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
# Two more pictures, told apart by their bytes: the one an automatic default makes, and one a person keeps by hand.
MADE_BY_DEFAULT = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
MADE_BY_HAND = b"\x89PNG\r\n\x1a\n" + b"\x03" * 64
PICTURE = "https://lh3.example.test/a/photo.png"
ACTION = {"x-azizam-user-action": "1"}
DEFAULT_PATH = "/me/api/profile/avatar/default"


def _session_for(who: dict[str, str]):
    def inner(_request: Request) -> AdminSession | None:
        subject = who.get("subject")
        if not subject:
            return None
        return AdminSession(subject=subject, email=f"{subject}@example.test", role="none",
                            issued_at=time.time(), expires_at=time.time() + 3600)
    return inner


def _validator(request: Request, _session: AdminSession):
    if request.headers.get("x-azizam-user-action") != "1":
        return JSONResponse({"error": {"code": "user_action_header_required"}}, status_code=403)
    return None


class FakeGeneration:
    """Stands in for the app's wiring: which route exists, how a job starts, and which pictures a person owns."""

    def __init__(self, tmp_path: Path, *, model: str | None = "picture-route") -> None:
        self.model = model
        self.started: list[tuple[str, str, str]] = []
        self.owner_of: dict[str, str] = {}
        self.files: dict[str, Path] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.busy = False
        # What a start does next: raise ``start_error``, or first run ``during_start`` (a choice that lands mid-start).
        self.start_error: Exception | None = None
        self.during_start: Callable[[str], None] | None = None
        # When set, the model lookup waits on it, so a test can hold one start while other loads arrive.
        self.model_gate: threading.Event | None = None
        # The one person whose model lookup waits on the gate; None makes every lookup wait.
        self.model_gate_for: str | None = None
        self.model_entered = threading.Event()
        self._dir = tmp_path / "library"
        self._dir.mkdir(exist_ok=True)

    def add_job_picture(self, owner: str, asset_id: str, data: bytes = PNG) -> None:
        """A picture the owner's own job made: it sits in the Library and belongs to the owner."""
        path = self._dir / f"{asset_id}.png"
        path.write_bytes(data)
        self.owner_of[asset_id] = owner
        self.files[asset_id] = path

    def finish_job(self, job_id: str, *, status: str = "succeeded", asset_id: str | None = None) -> None:
        """The job reaches a stop: its picture (when it made one) is in the Library already."""
        self.jobs[job_id]["status"] = status
        self.jobs[job_id]["asset_ids"] = [asset_id] if asset_id else []

    def as_generation(self) -> AvatarGeneration:
        def own_picture_file(person_id: str, asset_id: str) -> Path | None:
            if self.owner_of.get(asset_id) != person_id:
                return None
            return self.files[asset_id]

        def model_for(person_id: str) -> str | None:
            if self.model_gate is not None and self.model_gate_for in (None, person_id):
                self.model_entered.set()
                self.model_gate.wait(5)
            return self.model

        def start(_request: Request, person_id: str, model: str, prompt: str) -> dict[str, Any]:
            if self.during_start is not None:
                self.during_start(person_id)
            if self.start_error is not None:
                raise self.start_error
            self.started.append((person_id, model, prompt))
            job_id = f"job_{len(self.started)}"
            self.jobs[job_id] = {"owner": person_id, "status": "queued", "asset_ids": []}
            return {"job_id": job_id, "status": "queued"}

        def job_state(person_id: str, job_id: str) -> dict[str, Any] | None:
            job = self.jobs.get(job_id)
            if job is None or job["owner"] != person_id:
                return None
            return {"status": job["status"], "asset_ids": list(job["asset_ids"])}

        def busy() -> bool:
            # As in the app: picture work is busy while any of its jobs is still queued or running.
            return self.busy or any(job["status"] in {"queued", "running"} for job in self.jobs.values())

        return AvatarGeneration(model_for=model_for, start=start, own_picture_file=own_picture_file,
                                job_state=job_state, busy=busy)


class Rig:
    def __init__(self, tmp_path: Path, generation: FakeGeneration) -> None:
        self.db = tmp_path / "admin.db"
        admin_store.init_db(self.db)
        self.generation = generation
        self.who: dict[str, str] = {}

        async def fetcher(url: str) -> media_fetch.FetchedMedia:
            return media_fetch.FetchedMedia(SIGN_IN_PNG, "p.png", "image/png")

        app = FastAPI()
        app.include_router(create_me_avatar_router(
            session_provider=_session_for(self.who), mutation_validator=_validator,
            admin_db_path=self.db, picture_fetcher=fetcher, cache=PictureCache(),
            generation=generation.as_generation()))
        self.client = TestClient(app)

    def person(self, name: str, *, picture: str | None = None) -> str:
        user = admin_store.upsert_oidc_user("https://idp.example", name, email=f"{name}@example.test",
                                            display_name=name.title(), path=self.db)
        if picture is not None:
            avatars.record_sign_in_picture(user["id"], picture, path=self.db)
        return user["id"]

    def as_(self, person_id: str | None) -> "Rig":
        self.who.clear()
        if person_id:
            self.who["subject"] = person_id
        return self


@pytest.fixture
def generation(tmp_path: Path) -> FakeGeneration:
    return FakeGeneration(tmp_path)


@pytest.fixture
def rig(tmp_path: Path, generation: FakeGeneration) -> Rig:
    return Rig(tmp_path, generation)


# --- The prompt: a fixed template that never holds a person's data -------------------------------

def test_the_prompt_is_fixed_by_its_seed_and_holds_no_personal_data() -> None:
    personal = ["Alice Example", "alice@example.test", "alice", PICTURE, "lh3.example.test", "photo"]
    for seed in range(0, 40):
        prompt = avatar_generation.build_prompt(seed)
        assert prompt == avatar_generation.build_prompt(seed)
        for value in personal:
            assert value.lower() not in prompt.lower(), value
        assert "no face" in prompt.lower() and "no text" in prompt.lower()


def test_the_prompt_takes_a_seed_and_nothing_about_the_person() -> None:
    # The only input is the seed. A person's name, email or picture has no argument to reach it through.
    import inspect

    assert list(inspect.signature(avatar_generation.build_prompt).parameters) == ["seed"]
    seeds = {avatar_generation.new_seed() for _ in range(20)}
    assert all(isinstance(seed, int) and seed >= 0 for seed in seeds)
    assert len(seeds) > 1  # a new attempt is a new seed, not the same one again


def test_new_attempts_use_different_styles_across_seeds() -> None:
    prompts = {avatar_generation.build_prompt(seed) for seed in range(len(avatar_generation.STYLES))}
    assert len(prompts) == len(avatar_generation.STYLES)


# --- The route: the first image route that is available and permitted ---------------------------

@dataclass(frozen=True)
class FakeCapability:
    intent: str
    model: str | None
    available: bool = True
    policy_available: bool = True
    sort_key: tuple = ()


def test_the_route_is_the_first_available_image_capability() -> None:
    capabilities = [
        FakeCapability("chat.completions", "chat-a"),
        FakeCapability("image.generate", "image-off", available=False),
        FakeCapability("image.generate", None),
        FakeCapability("image.generate", "image-b", policy_available=False),
        FakeCapability("image.generate", "image-a"),
        FakeCapability("image.edit", "edit-a"),
    ]
    assert avatar_generation.pick_route(capabilities) == "image-a"


def test_no_available_image_capability_means_no_route() -> None:
    assert avatar_generation.pick_route([FakeCapability("chat.completions", "chat-a")]) is None
    assert avatar_generation.pick_route([FakeCapability("image.generate", "image-off", available=False)]) is None


# --- The routes -----------------------------------------------------------------------------------

def test_no_image_route_hides_the_button_and_starts_nothing(rig: Rig, generation: FakeGeneration) -> None:
    generation.model = None
    alice = rig.person("alice")
    client = rig.as_(alice).client
    assert client.get("/me/api/profile/avatar/generator").json() == {"available": False}
    response = client.post("/me/api/profile/avatar/generate", headers=ACTION)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "generator_unavailable"
    assert generation.started == []


def test_the_generator_says_it_is_available_when_a_route_exists(rig: Rig) -> None:
    alice = rig.person("alice")
    assert rig.as_(alice).client.get("/me/api/profile/avatar/generator").json() == {"available": True}


def test_a_person_starts_a_job_with_the_fixed_prompt_on_the_route(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice", picture=PICTURE)
    response = rig.as_(alice).client.post("/me/api/profile/avatar/generate", headers=ACTION)
    assert response.status_code == 202
    assert response.json()["job"]["job_id"] == "job_1"
    [(person, model, prompt)] = generation.started
    assert person == alice and model == "picture-route"
    assert "Alice" not in prompt and "alice" not in prompt and PICTURE not in prompt


def test_starting_needs_a_session_and_the_action_header(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    assert rig.as_(None).client.post("/me/api/profile/avatar/generate", headers=ACTION).status_code == 401
    assert rig.as_(alice).client.post("/me/api/profile/avatar/generate").status_code == 403
    assert generation.started == []


def test_a_person_keeps_their_own_job_picture_as_their_avatar(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    saved = client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    assert saved.status_code == 200
    body = saved.json()
    assert body["choice"] == "generated"
    assert body["picture_url"].startswith("/me/api/profile/avatar/picture?v=")
    assert body["sign_in_picture_url"] is None
    picture = client.get("/me/api/profile/avatar/picture")
    assert picture.status_code == 200 and picture.content == PNG
    assert picture.headers["content-type"] == "image/png"
    assert picture.headers["content-disposition"] == 'inline; filename="avatar"'


def test_a_person_cannot_keep_another_persons_job_picture(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    bob = rig.person("bob")
    generation.add_job_picture(alice, "img_alices")
    response = rig.as_(bob).client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alices"},
                                        headers=ACTION)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "generated_not_found"
    assert avatars.get(bob, path=rig.db)["choice"] == "auto"
    assert avatars.get(bob, path=rig.db)["generated_at"] is None


@pytest.mark.parametrize("body", [
    {"asset_id": "vid_1"},
    {"asset_id": "../../admin.db"},
    {"asset_id": 7},
    {"asset_id": "img_alice1", "url": "https://evil.example/x.png"},
    {},
])
def test_keeping_refuses_anything_but_one_picture_id(rig: Rig, generation: FakeGeneration, body) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    response = rig.as_(alice).client.post("/me/api/profile/avatar/generated", json=body, headers=ACTION)
    assert response.status_code in {400, 404}
    assert avatars.get(alice, path=rig.db)["choice"] == "auto"


def _make_admin(db: Path, person_id: str) -> None:
    # A global grant: an App-scoped one is not a control-plane role (admin_store.effective_admin_roles).
    admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=person_id, path=db)


def test_an_admin_sees_a_generated_picture_only_when_its_owner_chose_it(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    admin = rig.person("ada")
    _make_admin(rig.db, admin)
    generation.add_job_picture(alice, "img_alice1")
    rig.as_(alice).client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    rig.as_(alice).client.put("/me/api/profile/avatar", json={"choice": "v3"}, headers=ACTION)
    assert rig.as_(admin).client.get(f"/me/api/profile/avatar/picture?person={alice}").status_code == 404
    assert rig.as_(admin).client.get("/me/api/profile/avatars").json()["people"][alice]["picture_url"] is None
    rig.as_(alice).client.put("/me/api/profile/avatar", json={"choice": "generated"}, headers=ACTION)
    shown = rig.as_(admin).client.get(f"/me/api/profile/avatar/picture?person={alice}")
    assert shown.status_code == 200 and shown.content == PNG
    listed = rig.as_(admin).client.get("/me/api/profile/avatars").json()["people"][alice]
    assert listed["choice"] == "generated"
    assert listed["picture_url"].startswith(f"/me/api/profile/avatar/picture?person={alice}&v=")


def test_a_member_never_sees_another_persons_picture_even_when_chosen(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    bob = rig.person("bob")
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    assert rig.as_(bob).client.get(f"/me/api/profile/avatar/picture?person={alice}").status_code == 404
    assert alice not in rig.as_(bob).client.get("/me/api/profile/avatars").json()["people"]


def test_choosing_a_generated_picture_needs_one_to_be_stored(rig: Rig) -> None:
    alice = rig.person("alice")
    response = rig.as_(alice).client.put("/me/api/profile/avatar", json={"choice": "generated"}, headers=ACTION)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "generated_unavailable"
    assert avatars.get(alice, path=rig.db)["choice"] == "auto"


def test_remove_returns_to_the_sign_in_picture_when_there_is_one(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice", picture=PICTURE)
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    assert client.get("/me/api/profile/avatar").json()["choice"] == "generated"
    removed = client.delete("/me/api/profile/avatar/generated", headers=ACTION)
    assert removed.status_code == 200
    body = removed.json()
    assert body["choice"] == "auto"
    assert body["picture_url"].startswith("/me/api/profile/avatar/picture?v=")
    assert body["sign_in_picture_url"].startswith("/me/api/profile/avatar/picture?kind=sign-in&v=")
    assert client.get("/me/api/profile/avatar/picture").content == SIGN_IN_PNG
    assert avatars.get(alice, path=rig.db)["generated_at"] is None


def test_remove_returns_to_the_palette_when_there_is_no_picture(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    body = client.delete("/me/api/profile/avatar/generated", headers=ACTION).json()
    assert body == {"choice": "auto", "person_id": alice, "picture_url": None, "sign_in_picture_url": None,
                    "made_picture_url": None, "generating": False}
    assert client.get("/me/api/profile/avatar/picture").status_code == 404


def test_the_person_previews_the_kept_picture_while_another_avatar_is_shown(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    admin = rig.person("ada")
    _make_admin(rig.db, admin)
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    client.put("/me/api/profile/avatar", json={"choice": "v3"}, headers=ACTION)
    view = client.get("/me/api/profile/avatar").json()
    assert view["choice"] == "v3" and view["picture_url"] is None
    assert view["made_picture_url"].startswith("/me/api/profile/avatar/picture?kind=generated&v=")
    preview = client.get(view["made_picture_url"])
    assert preview.status_code == 200 and preview.content == PNG
    # Only the person sees the kept picture while they show another avatar; an admin sees the chosen one only.
    assert rig.as_(admin).client.get(f"/me/api/profile/avatar/picture?kind=generated&person={alice}").status_code == 404
    assert rig.as_(admin).client.get(f"/me/api/profile/avatar/picture?person={alice}").status_code == 404


def test_the_person_previews_the_sign_in_picture_while_the_kept_one_shows(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice", picture=PICTURE)
    admin = rig.person("ada")
    _make_admin(rig.db, admin)
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    view = client.get("/me/api/profile/avatar").json()
    assert view["choice"] == "generated" and view["picture_url"].startswith("/me/api/profile/avatar/picture?v=")
    preview = client.get(view["sign_in_picture_url"])
    assert preview.status_code == 200 and preview.content == SIGN_IN_PNG
    assert rig.as_(admin).client.get(f"/me/api/profile/avatar/picture?kind=sign-in&person={alice}").status_code == 404


def test_a_picture_from_an_older_template_is_not_shown(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    with sqlite3.connect(rig.db) as conn:
        conn.execute("UPDATE user_avatars SET generated_template='0' WHERE user_id=?", (alice,))
    assert client.get("/me/api/profile/avatar").json()["picture_url"] is None
    assert client.get("/me/api/profile/avatar/picture").status_code == 404


def test_the_copy_is_kept_per_person_and_template_in_the_avatar_cache(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    rig.as_(alice).client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    copy = avatars.generated_dir(alice, path=rig.db) / avatar_generation.TEMPLATE_VERSION
    assert copy.read_bytes() == PNG
    assert copy.parent.parent == rig.db.parent / "avatar-cache"
    # The copy outlives the job's own picture: the person may delete it from the Library.
    generation.files["img_alice1"].unlink()
    assert rig.as_(alice).client.get("/me/api/profile/avatar/picture").content == PNG


def test_removing_a_person_deletes_the_copy(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    rig.as_(alice).client.post("/me/api/profile/avatar/generated", json={"asset_id": "img_alice1"}, headers=ACTION)
    copy_dir = avatars.generated_dir(alice, path=rig.db)
    assert copy_dir.exists()
    avatars.remove_generated_files(alice, path=rig.db)
    assert not copy_dir.exists()


# --- The schema: an existing database gains the choice -------------------------------------------

def test_an_existing_database_accepts_the_generated_choice_after_upgrade(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("https://idp.example", "carol", email="carol@example.test",
                                        display_name="Carol", path=db)
    with sqlite3.connect(db) as conn:
        # What a version 18 database holds: the old word list, and no generated columns.
        conn.execute("DROP TABLE user_avatars")
        conn.execute(
            "CREATE TABLE user_avatars (user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
            "choice TEXT NOT NULL DEFAULT 'auto' CHECK (choice IN ('auto','picture','v1','v2','v3','v4','v5','v6','v7','v8')), "
            "picture_url TEXT, picture_set_at INTEGER, updated_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO user_avatars(user_id, choice, updated_at) VALUES(?, 'v3', 1)", (user["id"],))
        conn.execute("UPDATE schema_version SET version = 18")
        conn.commit()
    admin_store.init_db(db)
    assert avatars.get(user["id"], path=db)["choice"] == "v3"
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(user_avatars)")}
    assert {"default_job_id", "default_attempted_at"} <= columns
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE user_avatars SET choice='generated', generated_template='1', generated_at=2 WHERE user_id=?",
                     (user["id"],))
        assert conn.execute("SELECT choice FROM user_avatars").fetchone()[0] == "generated"


def test_the_make_job_forgets_its_template_prompt(monkeypatch) -> None:
    # The template is the gateway's words, not the person's: the job asks prompt memory to keep nothing of it.
    import proxy

    seen: dict[str, Any] = {}
    monkeypatch.setattr(proxy.admin_store, "get_user", lambda person_id, path: {"id": person_id})
    monkeypatch.setattr(proxy.me_make, "start_person_job",
                        lambda request, body, **kwargs: seen.update(body=dict(body)) or {"job_id": "job_1"})
    prompt = avatar_generation.build_prompt(3)
    proxy._avatar_start_job(None, "usr_alice", "picture-route", prompt)
    assert seen["body"]["prompt"] == prompt
    assert seen["body"]["intent"] == "image.generate" and seen["body"]["n"] == 1
    assert seen["body"]["extensions"] == {prompt_memory.NAMESPACE: {"mode": "forget"}}


# --- Remove, the read that races a choice, and the person's removal ----------------------------------------------

def _keep(client, asset_id: str = "img_alice1"):
    return client.post("/me/api/profile/avatar/generated", json={"asset_id": asset_id}, headers=ACTION)


def _default(client):
    return client.post("/me/api/profile/avatar/default", headers=ACTION)


def test_remove_leaves_no_made_picture_when_the_person_shows_the_sign_in_picture(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice", picture=PICTURE)
    generation.add_job_picture(alice, "img_alice1")
    client = rig.as_(alice).client
    _keep(client)
    assert client.put("/me/api/profile/avatar", json={"choice": "picture"}, headers=ACTION).status_code == 200
    removed = client.delete("/me/api/profile/avatar/generated", headers=ACTION).json()
    assert removed["choice"] == "picture"
    assert removed["made_picture_url"] is None
    stored = avatars.get(alice, path=rig.db)
    assert stored["generated_template"] is None and stored["generated_at"] is None
    assert client.get("/me/api/profile/avatar/picture?kind=generated").status_code == 404
    assert client.get("/me/api/profile/avatar/picture").content == SIGN_IN_PNG


def test_a_generated_picture_read_answers_as_a_new_one_would_when_the_choice_changes(
        rig: Rig, generation: FakeGeneration, monkeypatch) -> None:
    alice = rig.person("alice")
    generation.add_job_picture(alice, "img_alice1")
    _keep(rig.as_(alice).client)
    reading, release = threading.Event(), threading.Event()
    read = avatars.read_generated

    def slow_read(person_id: str, *, path: Path) -> bytes | None:
        data = read(person_id, path=path)
        reading.set()
        release.wait(5)
        return data

    monkeypatch.setattr(avatars, "read_generated", slow_read)

    async def scenario() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=rig.client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://avatar.test") as http:
            pending = asyncio.ensure_future(http.get("/me/api/profile/avatar/picture"))
            await asyncio.to_thread(reading.wait, 5)
            changed = await http.put("/me/api/profile/avatar", json={"choice": "auto"}, headers=ACTION)
            release.set()
            waited = await pending
        return waited.status_code, changed.status_code

    assert asyncio.run(scenario()) == (404, 200)


def test_removing_a_person_through_the_accounts_path_deletes_the_picture_the_gateway_made(
        rig: Rig, generation: FakeGeneration) -> None:
    from gateway import accounts

    alice = rig.person("alice")
    owner = rig.person("owner")
    generation.add_job_picture(alice, "img_alice1")
    _keep(rig.as_(alice).client)
    copy = avatars.generated_dir(alice, path=rig.db) / avatar_generation.TEMPLATE_VERSION
    assert copy.read_bytes() == PNG
    accounts.change_person(alice, owner, remove=True, path=rig.db)
    assert not copy.exists() and not copy.parent.exists()
    with sqlite3.connect(rig.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_avatars WHERE user_id=?", (alice,)).fetchone()[0] == 0


# --- The automatic default: one make job for a person with no picture and no choice -----------------------------

def test_the_first_load_makes_one_default_picture_for_a_person_with_no_picture(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    admin = rig.person("ada")
    _make_admin(rig.db, admin)
    client = rig.as_(alice).client
    first = _default(client)
    assert first.status_code == 200 and first.json()["generating"] is True
    _default(client)
    assert len(generation.started) == 1
    generation.add_job_picture(alice, "img_default1")
    generation.finish_job("job_1", asset_id="img_default1")
    settled = client.get("/me/api/profile/avatar").json()
    assert settled["choice"] == "generated" and settled["generating"] is False
    assert settled["picture_url"].startswith("/me/api/profile/avatar/picture?v=")
    assert client.get("/me/api/profile/avatar/picture").content == PNG
    # Visible to others like any chosen avatar.
    assert rig.as_(admin).client.get(f"/me/api/profile/avatar/picture?person={alice}").content == PNG


def test_a_person_with_a_sign_in_picture_is_never_given_a_default(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice", picture=PICTURE)
    response = _default(rig.as_(alice).client)
    assert response.status_code == 200 and response.json()["generating"] is False
    assert generation.started == []
    assert avatars.get(alice, path=rig.db)["default_attempted_at"] is None


def test_no_image_route_starts_nothing_and_leaves_the_default_for_a_later_load(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    generation.model = None
    _default(client)
    assert generation.started == []
    assert avatars.get(alice, path=rig.db)["default_attempted_at"] is None
    generation.model = "picture-route"
    _default(client)
    assert len(generation.started) == 1


def test_other_picture_work_defers_the_default_without_using_its_attempt(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    generation.busy = True
    _default(client)
    assert generation.started == []
    assert avatars.get(alice, path=rig.db)["default_attempted_at"] is None
    generation.busy = False
    _default(client)
    assert len(generation.started) == 1


def _as(person_id: str) -> dict[str, str]:
    """The action header and the person a request is for: the app in ``_anyone_app`` serves whoever is named."""
    return {**ACTION, "x-test-person": person_id}


def _session_from_header(request: Request) -> AdminSession | None:
    subject = request.headers.get("x-test-person")
    if not subject:
        return None
    return AdminSession(subject=subject, email=f"{subject}@example.test", role="none",
                        issued_at=time.time(), expires_at=time.time() + 3600)


def _anyone_app(rig: Rig, generation: FakeGeneration) -> FastAPI:
    """One app that serves any person named in the header, so loads by different people can overlap."""
    async def no_fetch(_url: str) -> media_fetch.FetchedMedia:
        raise media_fetch.MediaFetchError("the tests do not fetch provider pictures")

    app = FastAPI()
    app.include_router(create_me_avatar_router(
        session_provider=_session_from_header, mutation_validator=_validator, admin_db_path=rig.db,
        picture_fetcher=no_fetch, cache=PictureCache(), generation=generation.as_generation()))
    return app


def test_loads_by_different_people_at_once_start_one_default_and_the_rest_defer(
        rig: Rig, generation: FakeGeneration) -> None:
    alice, bob, carol = rig.person("alice"), rig.person("bob"), rig.person("carol")
    app = _anyone_app(rig, generation)
    gate = threading.Event()
    generation.model_gate = gate

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://avatar.test") as http:
            first = asyncio.ensure_future(http.post(DEFAULT_PATH, headers=_as(alice)))
            # Alice's load holds the one start while its model lookup runs.
            assert await asyncio.to_thread(generation.model_entered.wait, 5)
            others = await asyncio.gather(http.post(DEFAULT_PATH, headers=_as(bob)),
                                          http.post(DEFAULT_PATH, headers=_as(carol)))
            gate.set()
            return await first, others

    first_answer, (bob_answer, carol_answer) = asyncio.run(scenario())
    assert first_answer.json()["generating"] is True
    assert bob_answer.json()["generating"] is False and carol_answer.json()["generating"] is False
    assert avatars.get(bob, path=rig.db)["default_attempted_at"] is None
    assert avatars.get(carol, path=rig.db)["default_attempted_at"] is None
    assert [person for person, _model, _prompt in generation.started] == [alice]
    # Alice's job is queued, so picture work is busy and the others still defer.
    _default(rig.as_(carol).client)
    assert len(generation.started) == 1
    # Each of them gets the default on a later load, once the job before theirs has stopped.
    generation.finish_job("job_1")
    _default(rig.as_(bob).client)
    generation.finish_job("job_2")
    _default(rig.as_(carol).client)
    assert [person for person, _model, _prompt in generation.started] == [alice, bob, carol]


def test_a_manual_make_started_during_the_default_lookup_defers_the_default_and_keeps_its_attempt(
        rig: Rig, generation: FakeGeneration) -> None:
    alice, bob = rig.person("alice"), rig.person("bob")
    app = _anyone_app(rig, generation)
    gate = threading.Event()
    generation.model_gate, generation.model_gate_for = gate, alice

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://avatar.test") as http:
            default = asyncio.ensure_future(http.post(DEFAULT_PATH, headers=_as(alice)))
            # Alice's default holds the one slot while its model lookup runs, just before it would start.
            assert await asyncio.to_thread(generation.model_entered.wait, 5)
            # A manual make does not take that slot, so Bob's job starts while Alice's lookup still waits.
            manual = await http.post("/me/api/profile/avatar/generate", headers=_as(bob))
            gate.set()
            return await default, manual

    default_answer, manual_answer = asyncio.run(scenario())
    assert manual_answer.status_code == 202
    assert [person for person, _model, _prompt in generation.started] == [bob]
    assert default_answer.json()["generating"] is False
    assert avatars.get(alice, path=rig.db)["default_attempted_at"] is None
    # The attempt is intact, so Alice's default is made on a later load, once Bob's job has stopped.
    generation.finish_job("job_1")
    _default(rig.as_(alice).client)
    assert [person for person, _model, _prompt in generation.started] == [bob, alice]


@pytest.mark.parametrize(("code", "status"), [("make_unavailable", 503), ("make_busy", 503), ("too_many_jobs", 429)])
def test_a_capacity_refusal_defers_the_default_and_keeps_its_attempt(
        rig: Rig, generation: FakeGeneration, code: str, status: int) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    generation.start_error = PublicFailure(code, status, maximum=3)
    response = _default(client)
    assert response.status_code == 200 and response.json()["generating"] is False
    assert avatars.get(alice, path=rig.db)["default_attempted_at"] is None
    generation.start_error = None
    _default(client)
    assert len(generation.started) == 1


def test_a_default_whose_start_fails_for_another_reason_is_never_retried(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    generation.start_error = PublicFailure("make_queue_failed", 500)
    _default(client)
    assert avatars.get(alice, path=rig.db)["default_attempted_at"] is not None
    generation.start_error = None
    _default(client)
    assert generation.started == []


def test_a_default_that_fails_while_making_is_not_retried(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    _default(client)
    generation.finish_job("job_1", status="failed")
    settled = client.get("/me/api/profile/avatar").json()
    assert settled["generating"] is False and settled["choice"] == "auto" and settled["picture_url"] is None
    _default(client)
    assert len(generation.started) == 1


def test_a_person_who_removes_their_default_is_not_given_another(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    _default(client)
    generation.add_job_picture(alice, "img_default1")
    generation.finish_job("job_1", asset_id="img_default1")
    client.get("/me/api/profile/avatar")
    client.delete("/me/api/profile/avatar/generated", headers=ACTION)
    _default(client)
    assert len(generation.started) == 1
    assert client.get("/me/api/profile/avatar").json()["choice"] == "auto"


def test_a_manual_keep_during_a_pending_default_is_not_overwritten_and_remove_leaves_nothing(
        rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    _default(client)
    generation.add_job_picture(alice, "img_hand1", MADE_BY_HAND)
    assert _keep(client, "img_hand1").status_code == 200
    generation.add_job_picture(alice, "img_default1", MADE_BY_DEFAULT)
    generation.finish_job("job_1", asset_id="img_default1")
    assert client.get("/me/api/profile/avatar").json()["choice"] == "generated"
    assert client.get("/me/api/profile/avatar/picture").content == MADE_BY_HAND
    removed = client.delete("/me/api/profile/avatar/generated", headers=ACTION).json()
    assert removed["choice"] == "auto" and removed["made_picture_url"] is None
    assert client.get("/me/api/profile/avatar/picture").status_code == 404
    _default(client)
    assert len(generation.started) == 1


def test_a_keep_that_lands_while_the_default_is_finishing_is_not_overwritten(
        rig: Rig, generation: FakeGeneration, monkeypatch) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    _default(client)
    generation.add_job_picture(alice, "img_hand1", MADE_BY_HAND)
    generation.add_job_picture(alice, "img_default1", MADE_BY_DEFAULT)
    generation.finish_job("job_1", asset_id="img_default1")
    settling = threading.Thread(target=avatars.finish_default, args=(alice, "job_1", MADE_BY_DEFAULT),
                                kwargs={"path": rig.db})
    write_copy = avatars._write_copy

    def write_and_let_the_default_settle(person_id: str, data: bytes, *, path: Path) -> None:
        write_copy(person_id, data, path=path)
        if data == MADE_BY_HAND:
            # The default settles while the keep is writing. It must wait for the keep, then change nothing.
            settling.start()
            settling.join(1)

    monkeypatch.setattr(avatars, "_write_copy", write_and_let_the_default_settle)
    assert _keep(client, "img_hand1").status_code == 200
    settling.join(5)
    assert client.get("/me/api/profile/avatar/picture").content == MADE_BY_HAND


def test_remove_during_a_pending_default_means_the_default_never_appears(rig: Rig, generation: FakeGeneration) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    _default(client)
    assert client.delete("/me/api/profile/avatar/generated", headers=ACTION).status_code == 200
    generation.add_job_picture(alice, "img_default1", MADE_BY_DEFAULT)
    generation.finish_job("job_1", asset_id="img_default1")
    view = client.get("/me/api/profile/avatar").json()
    assert view["choice"] == "auto" and view["picture_url"] is None and view["generating"] is False
    assert client.get("/me/api/profile/avatar/picture").status_code == 404
    _default(client)
    assert len(generation.started) == 1


@pytest.mark.parametrize("choice", ["v3", "auto"])
def test_a_manual_choice_while_the_default_is_pending_drops_its_picture(rig: Rig, generation: FakeGeneration,
                                                                       choice: str) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    _default(client)
    assert client.put("/me/api/profile/avatar", json={"choice": choice}, headers=ACTION).status_code == 200
    generation.add_job_picture(alice, "img_default1", MADE_BY_DEFAULT)
    generation.finish_job("job_1", asset_id="img_default1")
    view = client.get("/me/api/profile/avatar").json()
    assert view["choice"] == choice and view["generating"] is False
    assert view["made_picture_url"] is None
    assert client.get("/me/api/profile/avatar/picture?kind=generated").status_code == 404


@pytest.mark.parametrize("manual", ["keep", "auto", "variant", "remove"])
def test_a_manual_choice_while_the_default_is_starting_leaves_no_pending_job(
        rig: Rig, generation: FakeGeneration, manual: str) -> None:
    alice = rig.person("alice")
    client = rig.as_(alice).client
    generation.add_job_picture(alice, "img_hand1", MADE_BY_HAND)
    generation.add_job_picture(alice, "img_default1", MADE_BY_DEFAULT)

    def choose_by_hand(person_id: str) -> None:
        if manual == "keep":
            avatars.store_generated(person_id, MADE_BY_HAND, path=rig.db)
        elif manual == "auto":
            avatars.set_choice(person_id, "auto", path=rig.db)
        elif manual == "variant":
            avatars.set_choice(person_id, "v3", path=rig.db)
        else:
            avatars.clear_generated(person_id, path=rig.db)

    generation.during_start = choose_by_hand
    response = _default(client)
    assert response.json()["generating"] is False
    assert avatars.get(alice, path=rig.db)["default_job_id"] is None
    generation.finish_job("job_1", asset_id="img_default1")
    assert client.get("/me/api/profile/avatar").json()["generating"] is False
    shown = client.get("/me/api/profile/avatar/picture")
    if manual == "keep":
        assert shown.content == MADE_BY_HAND
    else:
        assert shown.status_code == 404


# --- The gateway's own make runtime: a person's picture job is scheduled on the loop and runs ---------------------

@pytest.fixture
def real_make(tmp_path: Path, monkeypatch):
    """The gateway's make runtime on throwaway stores: the same path a person's make job takes."""
    import proxy
    from gateway import media_jobs, receipt_store

    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    media_jobs.init_db(tmp_path / "jobs.db")
    receipt_store.init_db(tmp_path / "receipts.db")
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(media_jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(receipt_store, "DB_PATH", tmp_path / "receipts.db")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {})
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {})
    alice = admin_store.upsert_oidc_user("https://idp.example", "alice", email="alice@example.test", path=db)
    return proxy, db, alice["id"], tmp_path / "jobs.db"


def _real_make_app(proxy, db: Path, person_id: str) -> FastAPI:
    generation = AvatarGeneration(model_for=lambda _person_id: "picture-route", start=proxy._avatar_start_job,
                                  own_picture_file=lambda _person_id, _asset_id: None)
    app = FastAPI()
    app.include_router(create_me_avatar_router(
        session_provider=_session_for({"subject": person_id}), mutation_validator=_validator,
        admin_db_path=db, generation=generation))
    return app


def test_a_person_picture_job_is_scheduled_on_the_loop_and_runs_to_its_picture(real_make, monkeypatch) -> None:
    from gateway import media_jobs

    proxy, db, alice, jobs_db = real_make

    async def fake_execute(self, payload):
        return [{"asset_id": "img_" + "0a" * 32, "media_type": "image/png"}]

    monkeypatch.setattr(proxy._SessionMakeRuntime, "execute", fake_execute)

    async def scenario() -> tuple[int, str, dict[str, Any] | None]:
        transport = httpx.ASGITransport(app=_real_make_app(proxy, db, alice))
        async with httpx.AsyncClient(transport=transport, base_url="http://avatar.test") as http:
            response = await http.post("/me/api/profile/avatar/generate", headers=ACTION)
            job_id = response.json().get("job", {}).get("job_id", "") if response.status_code == 202 else ""
            job = None
            for _ in range(500):
                job = media_jobs.get(job_id, path=jobs_db) if job_id else None
                if job is not None and media_jobs.is_terminal(job["status"]):
                    break
                await asyncio.sleep(0.01)
        return response.status_code, job_id, job

    status, job_id, job = asyncio.run(scenario())
    assert status == 202
    assert job is not None and job["status"] == "succeeded"
    assert job_id not in proxy._image_media_job_scheduled
    assert job_id not in proxy._SESSION_MAKE_RUNTIME._gates


def test_a_job_that_cannot_be_scheduled_outside_the_loop_leaves_no_marker_or_gate(real_make) -> None:
    proxy, _db, _alice, _jobs_db = real_make
    runtime = proxy._SessionMakeRuntime()
    with pytest.raises(RuntimeError):
        runtime.schedule("mj_unscheduled", lambda: None)
    assert "mj_unscheduled" not in proxy._image_media_job_scheduled
    assert "mj_unscheduled" not in runtime._gates


def test_a_person_job_that_cannot_be_scheduled_fails_at_once_and_keeps_no_prompt(real_make, monkeypatch) -> None:
    from gateway import media_jobs

    proxy, db, alice, jobs_db = real_make

    def broken_schedule(self, job_id, run):
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(proxy._SessionMakeRuntime, "schedule", broken_schedule)
    client = TestClient(_real_make_app(proxy, db, alice))
    response = client.post("/me/api/profile/avatar/generate", headers=ACTION)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "make_queue_failed"
    app_id = admin_store.get_session_app_for_user(alice, path=db)["id"]
    jobs = media_jobs.recent(10, app_ids=[app_id], path=jobs_db)
    assert [job["status"] for job in jobs] == ["failed"]
    assert proxy._SESSION_MAKE_RUNTIME.load_payload(jobs[0]["job_id"]) is None
