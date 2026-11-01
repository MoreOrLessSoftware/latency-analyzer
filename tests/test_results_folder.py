"""
Tests for analysing a results folder again. Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

from analyze_video import find_recording  # noqa: E402
from camera_mode import presented_fps  # noqa: E402


class ResultsFolderTest(unittest.TestCase):
    def test_finds_the_recording_named_after_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "run_1"
            folder.mkdir()
            for name in ("run_1.mp4", "run_1.camera.json", "other.mp4", "other.camera.json"):
                (folder / name).write_text("")
            self.assertEqual(find_recording(folder), folder / "run_1.mp4")

    def test_finds_the_only_recording_with_a_timing_file(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for name in ("clip.mp4", "clip.camera.json", "unrelated.mp4"):
                (folder / name).write_text("")
            self.assertEqual(find_recording(folder), folder / "clip.mp4")

    def test_refuses_empty_or_ambiguous_folders(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            with self.assertRaises(SystemExit):
                find_recording(folder)
            for name in ("a.mp4", "a.camera.json", "b.mp4", "b.camera.json"):
                (folder / name).write_text("")
            with self.assertRaises(SystemExit) as raised:
                find_recording(folder)
            self.assertIn("several recordings", str(raised.exception))

    def test_presented_rate_comes_from_the_filmed_stretch(self):
        # 100 fps until 5 s, then 97 fps; the recording ran from 6 to 9 s
        first = np.arange(0, 5, 0.01)
        second = 5 + np.arange(0, 6, 1 / 97)
        present = np.concatenate([first, second])
        host = types.SimpleNamespace(frames=np.column_stack([np.arange(present.size), present, present]),
                                     start_qpc=0, frequency=10_000_000)
        timing = types.SimpleNamespace(starts_ns=np.array([6e9, 9e9]))
        fit = types.SimpleNamespace(host_seconds=lambda ns: ns / 1e9)
        self.assertEqual(presented_fps(host, timing, fit), 97.0)
        empty = types.SimpleNamespace(frames=np.zeros((0, 3)), start_qpc=0, frequency=10_000_000)
        self.assertIsNone(presented_fps(empty, timing, fit))


class ChooseRunTest(unittest.TestCase):
    def make_runs(self, root):
        import json
        import os

        for i, name in enumerate(("old_run", "new_run")):
            folder = root / name
            folder.mkdir()
            for suffix in (".mp4", ".camera.json"):
                (folder / f"{name}{suffix}").write_text("")
            os.utime(folder / f"{name}.mp4", (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
        (root / "new_run" / "new_run.summary.json").write_text(json.dumps({"frames": {"median": 20.04}}))
        (root / "new_run" / "Moonlight-pacing-1.csv").write_text("")
        (root / "not_a_run").mkdir()

    def test_lists_runs_newest_first_with_what_they_hold(self):
        import analyze_video

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_runs(root)
            runs = analyze_video.list_runs([root])
            self.assertEqual([r.name for r in runs], ["new_run", "old_run"])
            line = analyze_video.describe_run(runs[0])
            self.assertIn("host present -> photon 20.0 ms", line)
            self.assertIn("[Moonlight trace]", line)
            self.assertIn("not analysed", analyze_video.describe_run(runs[1]))

    def test_asks_until_it_gets_a_number(self):
        import builtins
        from unittest import mock

        import analyze_video

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_runs(root)
            with mock.patch.object(analyze_video, "results_roots", return_value=[root]), \
                    mock.patch.object(builtins, "input", side_effect=["x", "9", "2"]), \
                    mock.patch("sys.stdout"):
                self.assertEqual(analyze_video.choose_run().name, "old_run")
            with mock.patch.object(analyze_video, "results_roots", return_value=[root]), \
                    mock.patch.object(builtins, "input", side_effect=[""]), mock.patch("sys.stdout"):
                self.assertIsNone(analyze_video.choose_run())
            with mock.patch.object(analyze_video, "results_roots", return_value=[]), mock.patch("sys.stdout"):
                self.assertIsNone(analyze_video.choose_run())


if __name__ == "__main__":
    unittest.main()
