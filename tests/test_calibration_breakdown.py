import unittest

from niome_subnet.genomics.validation.stage5 import calibration_breakdown


class CalibrationBreakdownTests(unittest.TestCase):
    def test_exposes_fields_needed_to_diagnose_confidence(self):
        bins = [{
            "bin": 8,
            "range": [0.8, 0.9],
            "n": 12,
            "mean_confidence": 0.84,
            "observed_rate": 0.75,
        }]
        reward = {
            "n_calibration_calls": 100,
            "brier_score": 0.12,
            "brier_skill_score": 0.35,
            "empirical_exact_match_rate": 0.78,
            "reliability_bins": bins,
        }

        self.assertEqual(reward, calibration_breakdown(reward))

    def test_defaults_preserve_compatibility_with_old_reward_artifacts(self):
        self.assertEqual(
            {
                "n_calibration_calls": 0,
                "brier_score": None,
                "brier_skill_score": None,
                "empirical_exact_match_rate": None,
                "reliability_bins": [],
            },
            calibration_breakdown({}),
        )


if __name__ == "__main__":
    unittest.main()
