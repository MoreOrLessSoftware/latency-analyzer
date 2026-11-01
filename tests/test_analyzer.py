"""
Tests for the report's chart helpers. Run with: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

from report import dot_plot, nice_step  # noqa: E402


class ReportTest(unittest.TestCase):
    def test_nice_step(self):
        self.assertEqual(nice_step(7), 1)
        self.assertEqual(nice_step(30), 5)
        self.assertEqual(nice_step(150), 20)

    def test_flip_chart_marks_each_flip_and_its_direction(self):
        flips = [
            {"input": 1, "latency": 20.5, "rising": True},
            {"input": 2, "latency": 22.6, "rising": False},
        ]
        svg = dot_plot(flips, 97)
        self.assertEqual(svg.count('class="dot"'), 1)
        self.assertEqual(svg.count('class="dot hollow"'), 1)
        self.assertIn("Flip 2: 22.60 ms, trigger turned black, 2.19 frames", svg)
        # Gridlines every stream frame, labelled
        self.assertIn("2 frames", svg)


if __name__ == "__main__":
    unittest.main()
