"""Tests for gateway.default_profiles — product-default profile seeding."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store, default_profiles  # noqa: E402


class SeedDefaultProfilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seeds_have_stable_policy_ids_and_names(self) -> None:
        ids = [
            seed["profile"]["id"] for seed in default_profiles.DEFAULT_PROFILE_SEEDS
        ]
        names = [seed["name"] for seed in default_profiles.DEFAULT_PROFILE_SEEDS]
        self.assertEqual(len(ids), len(set(ids)), "policy ids must be unique")
        self.assertEqual(len(names), len(set(names)), "names must be unique")
        for ident in ids:
            self.assertTrue(ident)
            self.assertIsInstance(ident, str)

    def test_fresh_db_gets_all_defaults(self) -> None:
        self.assertEqual(admin_store.list_profiles(path=self.db), [])

        created = default_profiles.seed_default_profiles(path=self.db)
        self.assertEqual(
            {row["name"] for row in created},
            {seed["name"] for seed in default_profiles.DEFAULT_PROFILE_SEEDS},
        )

        stored = admin_store.list_profiles(path=self.db)
        stored_policy_ids = {
            row["profile"]["id"] for row in stored if row.get("profile")
        }
        self.assertEqual(
            stored_policy_ids,
            {seed["profile"]["id"] for seed in default_profiles.DEFAULT_PROFILE_SEEDS},
        )

        # Each row should be a normal DB-owned profile (generated prof_ id +
        # the in-doc id preserved verbatim for policy lookups).
        for row in stored:
            self.assertTrue(row["id"].startswith("prof_"))
            self.assertEqual(
                row["profile"]["id"],
                next(
                    seed["profile"]["id"]
                    for seed in default_profiles.DEFAULT_PROFILE_SEEDS
                    if seed["name"] == row["name"]
                ),
            )
            self.assertTrue(row["description"])

    def test_second_seed_is_no_op(self) -> None:
        first = default_profiles.seed_default_profiles(path=self.db)
        self.assertEqual(len(first), len(default_profiles.DEFAULT_PROFILE_SEEDS))

        second = default_profiles.seed_default_profiles(path=self.db)
        self.assertEqual(second, [])

        stored = admin_store.list_profiles(path=self.db)
        self.assertEqual(len(stored), len(default_profiles.DEFAULT_PROFILE_SEEDS))

    def test_does_not_overwrite_operator_profile_with_same_policy_id(self) -> None:
        seed = default_profiles.DEFAULT_PROFILE_SEEDS[0]
        operator_doc = {
            "id": seed["profile"]["id"],
            "protocols": ["openai"],
            "status_visibility": {"level": "standard"},
        }
        operator_row = admin_store.create_profile(
            "Operator-owned",
            operator_doc,
            description="hand-edited",
            path=self.db,
        )

        created = default_profiles.seed_default_profiles(path=self.db)
        # The seed sharing this policy id must be skipped; the other defaults
        # should still be inserted.
        self.assertNotIn(
            seed["name"], {row["name"] for row in created}
        )
        self.assertEqual(
            len(created),
            len(default_profiles.DEFAULT_PROFILE_SEEDS) - 1,
        )

        # The operator's row is untouched.
        refreshed = admin_store.get_profile(operator_row["id"], path=self.db)
        assert refreshed is not None
        self.assertEqual(refreshed["name"], "Operator-owned")
        self.assertEqual(refreshed["description"], "hand-edited")
        self.assertEqual(
            refreshed["profile"]["status_visibility"]["level"], "standard"
        )

    def test_does_not_overwrite_operator_profile_with_same_name(self) -> None:
        seed = default_profiles.DEFAULT_PROFILE_SEEDS[1]
        existing = admin_store.create_profile(
            seed["name"],
            {"id": "operator-custom", "protocols": ["anthropic"]},
            path=self.db,
        )

        created = default_profiles.seed_default_profiles(path=self.db)
        self.assertNotIn(seed["name"], {row["name"] for row in created})

        refreshed = admin_store.get_profile(existing["id"], path=self.db)
        assert refreshed is not None
        self.assertEqual(refreshed["profile"]["id"], "operator-custom")

    def test_renamed_seed_still_skipped_on_resseed(self) -> None:
        # Seed once, rename a row, seed again — the renamed row should NOT
        # be re-inserted under its original name, because we dedupe on the
        # in-doc policy id too.
        default_profiles.seed_default_profiles(path=self.db)

        target_seed = default_profiles.DEFAULT_PROFILE_SEEDS[0]
        target = next(
            row
            for row in admin_store.list_profiles(path=self.db)
            if row["name"] == target_seed["name"]
        )
        admin_store.update_profile(
            target["id"], name="Renamed by operator", path=self.db
        )

        created = default_profiles.seed_default_profiles(path=self.db)
        self.assertEqual(created, [])

        stored = admin_store.list_profiles(path=self.db)
        self.assertEqual(len(stored), len(default_profiles.DEFAULT_PROFILE_SEEDS))
        names = {row["name"] for row in stored}
        self.assertIn("Renamed by operator", names)
        self.assertNotIn(target_seed["name"], names)

    def test_seeded_profiles_load_through_profile_loader(self) -> None:
        from gateway.profiles import load_profile

        default_profiles.seed_default_profiles(path=self.db)
        for row in admin_store.list_profiles(path=self.db):
            loaded = load_profile(row["profile"])
            self.assertEqual(loaded.id, row["profile"]["id"])


if __name__ == "__main__":
    unittest.main()
