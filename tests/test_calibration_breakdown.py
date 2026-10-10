import unittest
from unittest import mock

from niome_subnet.genomics.validation.stage5 import calibration_breakdown


class CalibrationBreakdownTests(unittest.TestCase):
    def test_exposes_fields_needed_to_diagnose_confidence(self):
        reward = {
            "n_calibration_calls": 100,
            "brier_score": 0.12,
            "brier_skill_score": 0.35,
            "empirical_exact_match_rate": 0.78,
        }

        self.assertEqual(reward, calibration_breakdown(reward))

    def test_defaults_preserve_compatibility_with_old_reward_artifacts(self):
        self.assertEqual(
            {
                "n_calibration_calls": 0,
                "brier_score": None,
                "brier_skill_score": None,
                "empirical_exact_match_rate": None,
            },
            calibration_breakdown({}),
        )

    def test_withholds_per_bin_reliability_table(self):
        """A bin the miner put one call into reports that call's label.

        The bin index is int(confidence * N_RELIABILITY_BINS), so the miner
        chooses which bin each of its calls lands in.
        """
        reward = {
            "n_calibration_calls": 100,
            "reliability_bins": [{
                "bin": 0, "range": [0.0, 0.1], "n": 1,
                "mean_confidence": 0.05, "observed_rate": 1.0,
            }],
        }

        breakdown = calibration_breakdown(reward)

        self.assertNotIn("reliability_bins", breakdown)
        self.assertNotIn(1.0, breakdown.values())

    def test_withholds_warnings_which_interpolate_a_secret_constant(self):
        reward = {
            "warnings": ["N=4 < MIN_CALIBRATION_N=30; failing to the floor"],
        }

        self.assertNotIn("warnings", calibration_breakdown(reward))


class BreakdownSchemaTests(unittest.TestCase):
    def test_invalid_submission_carries_the_same_breakdown_keys(self):
        """Every uid in a round must expose the same fields.

        A consumer that reads a Stage 4 field would otherwise crash on exactly
        the uids whose submission failed to validate.
        """
        from niome_subnet.genomics import validation

        with mock.patch.object(validation, "run_stage12",
                               side_effect=ValueError("malformed submission")):
            score = validation.benchmark_submission(7)

        self.assertTrue(score.breakdown["rejected"])
        self.assertEqual(0.0, score.final_score)
        for key in calibration_breakdown({}):
            self.assertIn(key, score.breakdown)


if __name__ == "__main__":
    unittest.main()
