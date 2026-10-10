import unittest

from niome_subnet.genomics.validation.stage4 import calibration_factor

# Representative Stage 4 contract block. The mapping is the documented linear
# one; FLOOR and MIN_CALIBRATION_N are the gate's own constants.
CONSTANTS = {
    "intercept": 0.5,
    "slope": 0.5,
    "MIN_CALIBRATION_N": 30,
    "FLOOR": 0.15,
}
FLOOR = CONSTANTS["FLOOR"]


class CalibrationFactorTests(unittest.TestCase):
    def test_honest_confidence_beats_overconfidence_on_a_perfect_submission(self):
        """A degenerate reference must not punish an honest 0.9.

        base = 1.0 makes base*(1-base) zero, so the skill score is undefined.
        Dividing by a clamped epsilon scored this submission -1e7 and clipped
        it to the floor, while the same submission declaring 1.0 scored 1.0 --
        overconfidence strictly dominating calibration.
        """
        honest = [(0.9, 1)] * 67
        overconfident = [(1.0, 1)] * 67

        honest_factor, _, base, bss, warnings = calibration_factor(
            honest, CONSTANTS, FLOOR)
        overconfident_factor, *_ = calibration_factor(
            overconfident, CONSTANTS, FLOOR)

        self.assertEqual(1.0, base)
        self.assertIsNone(bss)
        self.assertEqual(1.0, honest_factor)
        self.assertEqual(overconfident_factor, honest_factor)
        self.assertTrue(any("undefined" in w for w in warnings))

    def test_perfect_accuracy_still_fails_the_floor_below_min_n(self):
        """The N gate is ordered first, so the exception cannot be farmed."""
        factor, _, _, _, warnings = calibration_factor(
            [(0.9, 1)] * 5, CONSTANTS, FLOOR)

        self.assertEqual(FLOOR, factor)
        self.assertTrue(any("MIN_CALIBRATION_N" in w for w in warnings))

    def test_zero_match_rate_fails_the_floor(self):
        factor, _, base, bss, warnings = calibration_factor(
            [(0.9, 0)] * 40, CONSTANTS, FLOOR)

        self.assertEqual(0.0, base)
        self.assertIsNone(bss)
        self.assertEqual(FLOOR, factor)
        self.assertTrue(any("exact-match rate is 0" in w for w in warnings))

    def test_no_scored_calls_fails_the_floor(self):
        factor, brier, base, bss, warnings = calibration_factor(
            [], CONSTANTS, FLOOR)

        self.assertEqual(FLOOR, factor)
        self.assertEqual([None, None, None], [brier, base, bss])
        self.assertTrue(any("no scored calls" in w for w in warnings))

    def test_skill_score_rewards_the_better_calibrated_of_two_equal_miners(self):
        """Same accuracy, different honesty: 40 of 50 calls match."""
        calibrated = [(0.8, 1)] * 40 + [(0.8, 0)] * 10
        overconfident = [(1.0, 1)] * 40 + [(1.0, 0)] * 10

        calibrated_factor, _, _, calibrated_bss, _ = calibration_factor(
            calibrated, CONSTANTS, FLOOR)
        overconfident_factor, _, _, overconfident_bss, _ = calibration_factor(
            overconfident, CONSTANTS, FLOOR)

        self.assertGreater(calibrated_bss, overconfident_bss)
        self.assertGreater(calibrated_factor, overconfident_factor)
        self.assertLessEqual(calibrated_factor, 1.0)
        self.assertGreaterEqual(overconfident_factor, FLOOR)


if __name__ == "__main__":
    unittest.main()
