"""
Tests for the analyzer's pure helpers. Run with: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

from analyze_video import find_outliers  # noqa: E402
from report import nice_step, stacked_plot  # noqa: E402


class FindOutliersTest(unittest.TestCase):
    def test_flags_values_far_from_their_edge_median(self):
        values = [8.5, 8.7, 8.6, 8.8, 17.2, 8.4]
        rising = [True] * 6
        [(index, value, median)] = find_outliers(values, rising)
        self.assertEqual((index, value), (4, 17.2))
        self.assertAlmostEqual(median, 8.65)

    def test_judges_each_edge_on_its_own(self):
        # Falls read 10 ms later than rises across the board, which is no reason to flag them
        values = [8.5, 18.5, 8.6, 18.6, 8.7, 18.7, 8.8, 18.8]
        rising = [True, False] * 4
        self.assertEqual(find_outliers(values, rising), [])

    def test_skips_missing_values_and_small_groups(self):
        values = [8.5, None, 8.6, 30.0, 8.7, 8.8]
        rising = [True, True, True, True, True, False]
        outliers = find_outliers(values, rising)
        self.assertEqual([o[0] for o in outliers], [3])
        self.assertEqual(find_outliers([8.0, 30.0, 8.0], [True] * 3), [])

    def test_a_late_marker_that_evens_out_the_total_is_not_an_outlier(self):
        latency = [8.7, 8.6, 8.8, 17.2, 8.5, 8.7]
        marker = [14.5, 15.0, 14.0, 6.2, 14.8, 14.6]
        rising = [True] * 6
        self.assertEqual([o[0] for o in find_outliers(latency, rising)], [3])
        self.assertEqual(find_outliers([a + b for a, b in zip(latency, marker)], rising), [])


class ReportTest(unittest.TestCase):
    def test_nice_step(self):
        self.assertEqual(nice_step(7), 1)
        self.assertEqual(nice_step(30), 5)
        self.assertEqual(nice_step(150), 20)

    def test_stacked_plot_marks_unlogged_and_falling_presses(self):
        presses = [
            {"input": 1, "latency": 8.7, "rising": True, "marker_ms": 12.0},
            {"input": 2, "latency": 17.2, "rising": False, "marker_ms": 6.0},
            {"input": 3, "latency": 8.5, "rising": True, "marker_ms": None},
        ]
        svg = stacked_plot(presses, 117)
        self.assertEqual(svg.count('class="seg client"'), 2)
        self.assertEqual(svg.count('class="seg stream hollow"'), 1)
        self.assertEqual(svg.count('class="hit unlogged"'), 1)
        self.assertIn("key press to presented 20.70 ms", svg)
        self.assertIn("median", svg)


if __name__ == "__main__":
    unittest.main()
