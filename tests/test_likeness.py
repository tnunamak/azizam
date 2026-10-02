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
