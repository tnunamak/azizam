"""Likeness scoring: matching, margins, verdict bands and infant handling."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import likeness  # noqa: E402
from gateway.likeness import FaceRecord, LikenessError, Person  # noqa: E402


def unit(*values: float) -> tuple[float, ...]:
    norm = math.sqrt(sum(v * v for v in values))
    return tuple(v / norm for v in values)


def face(vec: tuple[float, ...], *, x: float = 0.0, width: float = 120.0, age: int | None = 40,
         det: float = 0.9, models: tuple[str, ...] = ("buffalo_l", "antelopev2")) -> FaceRecord:
    return FaceRecord(
        bbox=(x, 0.0, x + width, width),
        det_score=det,
        age=age,
        embeddings={m: vec for m in models},
    )


A = unit(1, 0, 0, 0)
B = unit(0, 1, 0, 0)
A_NEAR = unit(1, 0.4, 0, 0)       # cosine to A is about 0.93
A_FAR = unit(0.5, 0.9, 0, 0.3)    # weak resemblance to A


class CosineTests(unittest.TestCase):
    def test_identical_and_orthogonal(self) -> None:
        self.assertAlmostEqual(likeness.cosine(A, A), 1.0)
        self.assertAlmostEqual(likeness.cosine(A, B), 0.0)

    def test_zero_vector_scores_zero(self) -> None:
        self.assertEqual(likeness.cosine((0, 0), (1, 0)), 0.0)


class CheckLikenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Person("alice", (face(A),))
        self.bob = Person("bob", (face(B),))

    def test_matches_each_person_to_the_right_face_regardless_of_order(self) -> None:
        candidates = [face(B, x=0), face(A_NEAR, x=200)]
        report = likeness.check_likeness([self.alice, self.bob], candidates)
        by_label = {p["label"]: p for p in report["people"]}
        self.assertEqual(by_label["alice"]["match"]["candidate_face"], 1)
        self.assertEqual(by_label["bob"]["match"]["candidate_face"], 0)
        self.assertEqual(report["overall"]["status"], "pass")

    def test_two_people_cannot_claim_one_face(self) -> None:
        report = likeness.check_likeness([self.alice, self.bob], [face(A)])
        verdicts = {p["label"]: p["verdict"] for p in report["people"]}
        self.assertEqual(verdicts["alice"], "match")
        self.assertEqual(verdicts["bob"], "missing")
        self.assertEqual(report["overall"]["status"], "fail")

    def test_unrelated_face_is_a_mismatch(self) -> None:
        report = likeness.check_likeness([self.alice], [face(unit(0, 0, 1, 0))])
        self.assertEqual(report["people"][0]["verdict"], "mismatch")

    def test_borderline_score_needs_review(self) -> None:
        report = likeness.check_likeness([self.alice], [face(unit(0.38, 0.92, 0, 0))])
        self.assertEqual(report["people"][0]["verdict"], "uncertain")
        self.assertEqual(report["overall"]["status"], "review")

    def test_margin_blocks_a_face_that_resembles_everyone(self) -> None:
        near_both = unit(1, 1, 0, 0)
        close_to_alice = Person("alice", (face(unit(1, 0.9, 0, 0)),))
        close_to_bob = Person("bob", (face(unit(0.9, 1, 0, 0)),))
        report = likeness.check_likeness([close_to_alice, close_to_bob], [face(near_both), face(near_both, x=300)])
        self.assertNotEqual(report["overall"]["status"], "pass")

    def test_infant_match_is_downgraded_and_flagged(self) -> None:
        baby = Person("baby", (face(A, age=1),))
        report = likeness.check_likeness([baby], [face(A_NEAR)])
        entry = report["people"][0]
        self.assertTrue(entry["infant"])
        self.assertEqual(entry["reliability"], "low")
        self.assertEqual(entry["verdict"], "uncertain")
        self.assertTrue(report["overall"]["needs_human_check"])

    def test_infant_flag_can_be_overridden(self) -> None:
        person = Person("kid", (face(A, age=1),), infant=False)
        self.assertFalse(person.is_infant())

    def test_small_face_is_noted(self) -> None:
        report = likeness.check_likeness([self.alice], [face(A, width=30)])
        self.assertTrue(any("30px" in n for n in report["people"][0]["notes"]))

    def test_best_reference_face_counts(self) -> None:
        person = Person("alice", (face(B), face(A)))
        report = likeness.check_likeness([person], [face(A)])
        self.assertAlmostEqual(report["people"][0]["match"]["score"], 1.0, places=3)

    def test_models_are_averaged(self) -> None:
        ref = FaceRecord((0, 0, 100, 100), 0.9, 40, {"buffalo_l": A, "antelopev2": A})
        cand = FaceRecord((0, 0, 100, 100), 0.9, 40, {"buffalo_l": A, "antelopev2": B})
        report = likeness.check_likeness([Person("p", (ref,))], [cand])
        self.assertAlmostEqual(report["people"][0]["match"]["score"], 0.5, places=3)

    def test_errors(self) -> None:
        with self.assertRaises(LikenessError):
            likeness.check_likeness([], [face(A)])
        with self.assertRaises(LikenessError):
            likeness.check_likeness([self.alice] * 9, [face(A)])

    def test_no_candidate_faces_reports_missing(self) -> None:
        report = likeness.check_likeness([self.alice], [])
        self.assertEqual(report["people"][0]["verdict"], "missing")


class ExtraFaceTests(unittest.TestCase):
    def test_unclaimed_face_is_listed_and_can_block_the_pass(self) -> None:
        alice = Person("alice", (face(A),))
        stranger = face(B, x=300)
        allowed = likeness.check_likeness([alice], [face(A), stranger])
        self.assertEqual(allowed["overall"]["extra_faces"], 1)
        self.assertEqual(allowed["overall"]["status"], "pass")
        strict = likeness.check_likeness([alice], [face(A), stranger], allow_extra_faces=False)
        self.assertFalse(strict["overall"]["all_meet_threshold"])
        self.assertEqual(strict["overall"]["status"], "review")

    def test_tiny_unclaimed_faces_are_ignored(self) -> None:
        report = likeness.check_likeness([Person("a", (face(A),))], [face(A), face(B, width=20)])
        self.assertEqual(report["overall"]["extra_faces"], 0)

    def test_rank_key_penalises_extras_only_when_asked(self) -> None:
        clean = {"people": [{"match": {"score": 0.5}}], "extra_faces": []}
        crowded = {"people": [{"match": {"score": 0.7}}], "extra_faces": [{}]}
        self.assertGreater(likeness.rank_key(clean, penalise_extra_faces=True), likeness.rank_key(crowded, penalise_extra_faces=True))
        self.assertGreater(likeness.rank_key(crowded), likeness.rank_key(clean))


class PoolingTests(unittest.TestCase):
    def test_best_versus_mean_over_several_reference_photos(self) -> None:
        person = Person("p", (face(A), face(B)))          # two reference photos, one matches
        best = likeness.check_likeness([person], [face(A)], pooling="best")
        mean = likeness.check_likeness([person], [face(A)], pooling="mean")
        self.assertAlmostEqual(best["people"][0]["match"]["score"], 1.0, places=3)
        self.assertAlmostEqual(mean["people"][0]["match"]["score"], 0.5, places=3)
        self.assertEqual(best["people"][0]["match"]["per_reference"], [1.0, 0.0])
        self.assertEqual(mean["pooling"], "mean")

    def test_unknown_pooling_is_an_error(self) -> None:
        with self.assertRaises(LikenessError):
            likeness.check_likeness([Person("p", (face(A),))], [face(A)], pooling="median")

    def test_profile_heads_get_a_note(self) -> None:
        kps = ((30, 50.0), (90, 50.0), (110, 80.0), (35, 110.0), (85, 110.0))   # nose far past the eyes
        profile = FaceRecord((0, 0, 120, 120), 0.9, 40, {"buffalo_l": A, "antelopev2": A}, kps=kps)
        report = likeness.check_likeness([Person("p", (face(A),))], [profile])
        self.assertTrue(any("profile" in n for n in report["people"][0]["notes"]))


class FacingTests(unittest.TestCase):
    @staticmethod
    def posed(x: float, nose_dx: float, vec: tuple[float, ...]) -> FaceRecord:
        kps = ((x + 30, 50.0), (x + 90, 50.0), (x + 60 + nose_dx, 80.0), (x + 35, 110.0), (x + 85, 110.0))
        return FaceRecord((x, 0, x + 120, 120), 0.9, 40, {"buffalo_l": vec, "antelopev2": vec}, kps=kps)

    def test_yaw_sign(self) -> None:
        self.assertGreater(self.posed(0, 20, A).yaw, 0.3)
        self.assertLess(self.posed(0, -20, A).yaw, -0.3)
        self.assertAlmostEqual(self.posed(0, 0, A).yaw, 0.0)
        self.assertIsNone(face(A).yaw)

    def test_facing_each_other(self) -> None:
        people = [Person("l", (face(A),)), Person("r", (face(B),))]
        toward = [self.posed(0, 20, A), self.posed(300, -20, B)]
        away = [self.posed(0, -20, A), self.posed(300, 20, B)]
        self.assertTrue(likeness.check_likeness(people, toward)["overall"]["facing_each_other"])
        self.assertFalse(likeness.check_likeness(people, away)["overall"]["facing_each_other"])
        self.assertIsNone(likeness.check_likeness(people[:1], toward[:1])["overall"]["facing_each_other"])

    def test_rank_key_prefers_facing_when_asked(self) -> None:
        yes = {"people": [{"match": {"score": 0.4}}], "overall": {"facing_each_other": True}}
        no = {"people": [{"match": {"score": 0.8}}], "overall": {"facing_each_other": False}}
        self.assertGreater(likeness.rank_key(yes, want_facing=True), likeness.rank_key(no, want_facing=True))
        self.assertGreater(likeness.rank_key(no), likeness.rank_key(yes))


class MergeAndRankTests(unittest.TestCase):
    def test_merge_models_joins_by_overlap(self) -> None:
        a = [{"bbox": [0, 0, 100, 100], "det_score": 0.9, "age": 30, "embedding": [1, 0]}]
        b = [{"bbox": [2, 2, 102, 102], "det_score": 0.9, "age": 30, "embedding": [0, 1]}]
        merged = likeness.merge_models({"buffalo_l": a, "antelopev2": b})
        self.assertEqual(set(merged[0].embeddings), {"buffalo_l", "antelopev2"})

    def test_rank_key_prefers_the_better_worst_person(self) -> None:
        good = {"people": [{"match": {"score": 0.6}}, {"match": {"score": 0.55}}]}
        lopsided = {"people": [{"match": {"score": 0.9}}, {"match": {"score": 0.2}}]}
        self.assertGreater(likeness.rank_key(good), likeness.rank_key(lopsided))


if __name__ == "__main__":
    unittest.main()
