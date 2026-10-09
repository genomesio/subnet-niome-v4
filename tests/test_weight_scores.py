import math
import unittest
from types import SimpleNamespace

import numpy as np

from niome_subnet.utils.weight_utils import (
    miner_score_fraction,
    process_scores_top,
    score_fractions_by_uid,
)


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

    def test_invalid_score_fractions_fail_to_zero(self):
        invalid = (None, True, -0.1, 1.1, math.nan, math.inf, "not-a-number")

        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(0.0, miner_score_fraction(score(0, value)))

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
