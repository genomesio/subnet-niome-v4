import logging
import math
import unittest
from types import SimpleNamespace

import numpy as np

from niome_subnet.utils.weight_utils import (
    miner_score_fraction,
    process_scores_top,
    score_fractions_by_uid,
)

# These tests exercise the rejection paths on purpose, and each one logs.  A
# NullHandler keeps logging's lastResort from dumping them to stderr; tests that
# care about a specific message use assertLogs, which installs its own handler.
logging.getLogger("niome_subnet.utils.weight_utils").addHandler(
    logging.NullHandler())


def score(uid, score_fraction, final_score=0.0):
    return SimpleNamespace(
        uid=uid,
        final_score=final_score,
        breakdown={"score_fraction": score_fraction},
    )


class ComparableScoreTests(unittest.TestCase):
    def test_uses_score_fraction_instead_of_absolute_final_score(self):
        scores = [
            score(0, 0.50, final_score=200.0),
            score(1, 0.80, final_score=100.0),
        ]

        result = score_fractions_by_uid(scores, 2)

        np.testing.assert_allclose(result, [0.50, 0.80])
        self.assertGreater(result[1], result[0])
        weights = process_scores_top(result)
        self.assertGreater(weights[1], weights[0])

    def test_structurally_invalid_score_fractions_fail_to_zero(self):
        invalid = (None, True, math.nan, math.inf, -math.inf, "not-a-number")

        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(0.0, miner_score_fraction(score(0, value)))

    def test_missing_breakdown_or_key_fails_to_zero(self):
        self.assertEqual(0.0, miner_score_fraction(
            SimpleNamespace(uid=0, breakdown={})))
        self.assertEqual(0.0, miner_score_fraction(
            SimpleNamespace(uid=0, breakdown={"final_score": 1.0})))
        self.assertEqual(0.0, miner_score_fraction(
            SimpleNamespace(uid=0, breakdown=None)))
        self.assertEqual(0.0, miner_score_fraction(SimpleNamespace(uid=0)))

    def test_missing_key_is_logged_rather_than_silently_zeroed(self):
        # A round-wide zero routes ~100% of the weight to the owner hotkey, so a
        # renamed key must not be indistinguishable from every miner failing.
        with self.assertLogs(
                "niome_subnet.utils.weight_utils", level="ERROR") as captured:
            miner_score_fraction(SimpleNamespace(uid=7, breakdown={}))

        self.assertIn("score_fraction", captured.output[0])
        self.assertIn("7", captured.output[0])

    def test_out_of_range_fractions_are_clamped_not_zeroed(self):
        # An over-range fraction can only come from contract credit weights that
        # exceed the ceiling, and it hits the best miners first.  Zeroing them
        # would invert the ranking and hand the round to the weakest.
        self.assertEqual(1.0, miner_score_fraction(score(0, 1.1)))
        self.assertEqual(1.0, miner_score_fraction(score(0, 42.0)))
        self.assertEqual(0.0, miner_score_fraction(score(0, -0.1)))

    def test_clamped_leader_still_outranks_in_range_miners(self):
        result = score_fractions_by_uid(
            [score(0, 0.60), score(1, 1.30)], 2)

        np.testing.assert_allclose(result, [0.60, 1.0])
        weights = process_scores_top(result)
        self.assertGreater(weights[1], weights[0])

    def test_accepts_numpy_integer_uids(self):
        result = score_fractions_by_uid(
            [score(np.int64(1), 0.9), score(np.int32(2), 0.4)], 4)

        np.testing.assert_allclose(result, [0.0, 0.9, 0.4, 0.0])

    def test_skips_non_integer_uids(self):
        result = score_fractions_by_uid(
            [score(None, 0.9), score("1", 0.9), score(1.5, 0.9),
             score(True, 0.9), score(1, 0.7)],
            3,
        )

        np.testing.assert_allclose(result, [0.0, 0.7, 0.0])

    def test_ignores_uids_outside_the_metagraph(self):
        result = score_fractions_by_uid(
            [score(-1, 0.9), score(1, 0.7), score(3, 0.8)],
            3,
        )

        np.testing.assert_allclose(result, [0.0, 0.7, 0.0])

    def test_rejects_negative_array_size(self):
        with self.assertRaises(ValueError):
            score_fractions_by_uid([], -1)


if __name__ == "__main__":
    unittest.main()
