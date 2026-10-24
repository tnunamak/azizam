"""A person's generated avatar: the fixed prompt, the image route it uses, the job, the keep and the remove."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, avatar_generation, avatars, media_fetch
from gateway.admin_oidc import AdminSession
from gateway.me_avatar import AvatarGeneration, PictureCache, create_me_avatar_router

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
# The sign-in picture the fake provider serves: a different image from the one the gateway made.
SIGN_IN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
PICTURE = "https://lh3.example.test/a/photo.png"
ACTION = {"x-azizam-user-action": "1"}


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
        self._dir = tmp_path / "library"
        self._dir.mkdir(exist_ok=True)

    def add_job_picture(self, owner: str, asset_id: str, data: bytes = PNG) -> None:
        """A picture the owner's own job made: it sits in the Library and belongs to the owner."""
        path = self._dir / f"{asset_id}.png"
        path.write_bytes(data)
        self.owner_of[asset_id] = owner
        self.files[asset_id] = path

    def as_generation(self) -> AvatarGeneration:
        def own_picture_file(person_id: str, asset_id: str) -> Path | None:
            if self.owner_of.get(asset_id) != person_id:
                return None
            return self.files[asset_id]

        def start(_request: Request, person_id: str, model: str, prompt: str) -> dict[str, Any]:
            self.started.append((person_id, model, prompt))
            return {"job_id": f"job_{len(self.started)}", "status": "queued"}

        return AvatarGeneration(model_for=lambda _person_id: self.model, start=start,
                                own_picture_file=own_picture_file)


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
                    "made_picture_url": None}
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
        conn.execute("UPDATE user_avatars SET choice='generated', generated_template='1', generated_at=2 WHERE user_id=?",
                     (user["id"],))
        assert conn.execute("SELECT choice FROM user_avatars").fetchone()[0] == "generated"
