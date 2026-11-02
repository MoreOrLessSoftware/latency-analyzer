"""
Tests for finding the pattern and the report's chart helpers. Run with: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

from analyze_video import find_fiducials  # noqa: E402
from report import dot_plot, nice_step  # noqa: E402


def bullseye(image, x, y, white):
    """Draw a bullseye centred at x, y: a white square, a black ring and a white center."""
    image[y - 30:y + 30, x - 30:x + 30] = white
    image[y - 20:y + 20, x - 20:x + 20] = 0
    image[y - 7:y + 7, x - 7:x + 7] = white


class FiducialTest(unittest.TestCase):
    def test_finds_dim_bullseyes_beside_a_bright_trigger(self):
        # As filmed at 4K: the bullseyes' whites well below the trigger's, and one corner dimmer still, so no single
        # threshold level finds all four
        image = np.full((540, 960), 150, np.uint8)
        image[200:340, 410:550] = 250
        corners = [(100, 60), (860, 60), (100, 480), (860, 480)]
        for (x, y), white in zip(corners, (205, 200, 200, 162)):
            bullseye(image, x, y, white)
        points = find_fiducials(image)
        self.assertIsNotNone(points)
        np.testing.assert_allclose(points, np.array(corners, np.float32), atol=1)

    def test_needs_all_four(self):
        image = np.full((540, 960), 150, np.uint8)
        for x, y in ((860, 60), (100, 480), (860, 480)):
            bullseye(image, x, y, 220)
        self.assertIsNone(find_fiducials(image))

    def test_ignores_a_stray_shape_in_the_pattern(self):
        # A bullseye-like shape away from the corners doesn't make a display outline, so the four corners win
        image = np.full((540, 960), 150, np.uint8)
        corners = [(100, 60), (860, 60), (100, 480), (860, 480)]
        for x, y in corners + [(480, 380)]:
            bullseye(image, x, y, 220)
        np.testing.assert_allclose(find_fiducials(image), np.array(corners, np.float32), atol=1)
        # With a corner hidden, the stray shape and the other three don't pass for one
        image[30:91, 70:131] = 150
        self.assertIsNone(find_fiducials(image))


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
