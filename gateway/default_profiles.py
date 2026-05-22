"""Product-default runtime profiles seeded into ``admin.db`` on first run.

The gateway ships with a small, opinionated set of editable profiles so the
``/admin`` dashboard isn't an empty room on a fresh install. These are normal
DB-owned profile rows after seeding — operators rename, edit, or delete them
through the same admin Profile APIs they use for any other profile.

Design notes:

* The built-in :data:`gateway.policy.DEFAULT_PROFILE` (id ``default-local``)
  remains the in-code fallback. Seeding does not replace it; it only ensures
  a freshly initialized ``admin.db`` has visible, editable starting points.
* Each default has a stable ``id`` inside its ``profile_json`` so callers
  (mappings, apps) can reference it by id without depending on the row's
  generated ``prof_…`` primary key.
* Seeding is idempotent in two ways: same row ``name`` is skipped, and the
  same in-doc profile id (already present in any existing row) is skipped.
* No profile is ever overwritten. An operator who renames or edits a default
  keeps their changes across restarts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway import admin_store


__all__ = [
    "DEFAULT_PROFILE_SEEDS",
    "seed_default_profiles",
]


# Each seed is the exact payload create_profile would accept: a name, a
# human description, and the profile_json doc the policy evaluator loads.
#
# Keep this list tiny. Three buckets cover what a self-hoster actually
# distinguishes between on day one:
#   * the box owner (you, with the keys to everything)
#   * a chat/RAG client (LLM assistant + embeddings, no media)
#   * a media-capable client (chat + image/audio/video)
DEFAULT_PROFILE_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "name": "Owner (local)",
        "description": (
            "Full access to local-only workflows. Use this for your own "
            "personal clients running on the same network."
        ),
        "profile": {
            "id": "owner-local",
            "status_visibility": {"level": "full"},
            "fallback": {"cloud_allowed": False},
        },
    },
    {
        "name": "Assistant (chat + RAG)",
        "description": (
            "Chat, embeddings, and text completions. No image, audio, or "
            "video generation. Good default for editor copilots and "
            "retrieval-augmented assistants."
        ),
        "profile": {
            "id": "assistant-chat",
            "protocols": ["openai", "anthropic", "ollama"],
            "allow": {
                "intents": [
                    "chat",
                    "text.completions",
                    "text.embeddings",
                    "models.list",
                ],
            },
            "status_visibility": {"level": "basic"},
            "fallback": {"cloud_allowed": False},
        },
    },
    {
        "name": "Media creator",
        "description": (
            "Chat plus image, audio, and video generation. Use for clients "
            "that need the full creative pipeline but should still see only "
            "basic gateway status."
        ),
        "profile": {
            "id": "media-creator",
            "protocols": ["openai", "anthropic", "ollama"],
            "allow": {
                "intents": [
                    "chat",
                    "text.completions",
                    "text.embeddings",
                    "image.generate",
                    "image.edit",
                    "speech.synthesis",
                    "speech.transcription",
                    "audio.generate",
                    "video.generate",
                    "models.list",
                ],
            },
            "status_visibility": {"level": "basic"},
            "fallback": {"cloud_allowed": False},
        },
    },
)


def _existing_profile_ids(
    profiles: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return the set of in-doc profile ids already present in the DB.

    The store's row id is generated (``prof_…``); the *policy* id is what
    operators reference. We dedupe against the policy id so re-running the
    seeder never inserts a duplicate even if a row was renamed.
    """
    ids: set[str] = set()
    for row in profiles:
        doc = row.get("profile")
        if isinstance(doc, Mapping):
            policy_id = doc.get("id")
            if isinstance(policy_id, str) and policy_id:
                ids.add(policy_id)
    return ids


def _existing_profile_names(
    profiles: Sequence[Mapping[str, Any]],
) -> set[str]:
    names: set[str] = set()
    for row in profiles:
        name = row.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def seed_default_profiles(
    *,
    path: Path = admin_store.DB_PATH,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Insert any missing default profiles into ``admin.db``.

    Returns the list of rows actually created (empty on a no-op second run).
    Existing profiles — including renamed or edited defaults — are never
    overwritten. A seed is considered "already present" if either its
    in-doc ``profile.id`` or its ``name`` matches an existing row.
    """
    existing = admin_store.list_profiles(path=path)
    seen_ids = _existing_profile_ids(existing)
    seen_names = _existing_profile_names(existing)

    created: list[dict[str, Any]] = []
    for seed in DEFAULT_PROFILE_SEEDS:
        profile_doc = seed["profile"]
        policy_id = profile_doc.get("id")
        name = seed["name"]
        if policy_id in seen_ids or name in seen_names:
            continue
        row = admin_store.create_profile(
            name,
            profile_doc,
            description=seed.get("description"),
            now=now,
            path=path,
        )
        created.append(row)
        seen_ids.add(policy_id)
        seen_names.add(name)
    return created
