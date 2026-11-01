"""
Tests for joining the camera's frames with Sunshine's frame log and Moonlight's pacing trace.
Run with: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

import traces  # noqa: E402

START_US = 50_000_000.0  # the host log's start, in QPC microseconds
CLIENT_OFFSET_US = 765_000_000.0  # Moonlight's clock minus the host's


def make_run(count=40, photon_ms=20.0, capture_ms=0.7, through_ms=11.0):
    """Camera frames, Sunshine rows and Moonlight rows for frames every 10 ms, with known stage times."""
    frames, sunshine, moonlight = [], [], []
    for n in range(count):
        present_s = 1.0 + n * 0.01
        capture_us = START_US + present_s * 1e6 + capture_ms * 1000
        rtp = 600_000 + n * 10_000
        frames.append({"frame": n, "present_s": present_s, "photon_s": present_s + photon_ms / 1000,
                       "latency": photon_ms, "rising": n % 2 == 0})
        sunshine.append({"rtp_us": str(rtp), "present_us": f"{capture_us:.0f}", "encoded_us": "4090", "sent_us": "4120"})
        present_client = capture_us + through_ms * 1000 + CLIENT_OFFSET_US
        moonlight.append({"host_us": str(rtp), "first_packet_us": f"{present_client - 9540:.0f}",
                          "last_packet_us": f"{present_client - 6410:.0f}", "decoded_us": f"{present_client - 5270:.0f}",
                          "arrival_us": f"{present_client - 1130:.0f}", "present_us": f"{present_client:.0f}",
                          # Moonlight's capture to present, from its clock sync
                          "capture_to_present_us": f"{through_ms * 1000:.0f}"})
    # Captures of other desktop frames, between the test app's, are skipped by the matching
    sunshine.append({"rtp_us": "1", "present_us": f"{START_US + 0.5e6:.0f}", "encoded_us": "4000", "sent_us": "4000"})
    return frames, sunshine, moonlight


def write_csv(path, rows, comment="# a comment line\n"):
    keys = list(rows[0].keys())
    lines = [comment, ",".join(keys)] + [",".join(r[k] for k in keys) for r in rows]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TracesTest(unittest.TestCase):
    def test_matches_each_present_to_its_capture(self):
        captures = [1000.0, 11_000.0, 21_000.0]
        self.assertEqual(traces.match_captures([500.0, 10_800.0, 20_000.0, 40_000.0], captures), [0, 1, 2, -1])
        # A capture more than 2 ms before the present is an earlier frame's
        self.assertEqual(traces.match_captures([3500.0], captures), [1])

    def test_joins_and_measures_every_stage(self):
        frames, sunshine, moonlight = make_run()
        joined = traces.join(frames, START_US, sunshine, moonlight)
        self.assertEqual(len(joined), 40)
        pipeline = traces.analyse(joined, len(frames), "s.csv", "m.csv")
        stages = {s.label: s.stats() for s in pipeline.stages}
        self.assertAlmostEqual(stages["host present -> capture"]["median"], 0.7, places=2)
        self.assertAlmostEqual(stages["capture -> encoded"]["median"], 4.09)
        self.assertAlmostEqual(stages["first packet -> last packet"]["median"], 3.13, places=2)
        self.assertAlmostEqual(stages["handed over -> GPU decode finished"]["median"], 4.14, places=2)
        self.assertAlmostEqual(stages["first packet -> client present"]["median"], 9.54, places=2)
        self.assertAlmostEqual(stages["capture -> photon, to white"]["median"], 19.3, places=2)
        self.assertEqual(stages["host present -> photon, to black"]["n"], 20)
        # Moonlight's capture to present, and what the camera adds after it, add up to capture to photon
        self.assertAlmostEqual(stages["capture -> client present (Moonlight's capture to present)"]["median"], 11.0, places=2)
        self.assertAlmostEqual(stages["client present -> photon, to white"]["median"], 19.3 - 11.0, places=2)
        self.assertAlmostEqual(stages["capture -> first packet"]["median"], 11.0 - 9.54, places=2)
        # Every frame keeps the same timing, so the cross-checks show no spread at all
        self.assertLess(pipeline.checks["moonlight_capture_to_present_sd_ms"], 0.01)
        self.assertLess(pipeline.checks["photon_minus_present_sd_to_white_ms"], 0.01)
        self.assertEqual(pipeline.notes, [])
        self.assertEqual(pipeline.summary()["joined"], 40)

    def test_leaves_out_stages_without_their_columns(self):
        frames, sunshine, moonlight = make_run()
        for row in moonlight:
            del row["decoded_us"]
        pipeline = traces.analyse(traces.join(frames, START_US, sunshine, moonlight), len(frames), "s", "m")
        labels = [s.label for s in pipeline.stages]
        self.assertNotIn("last packet -> decoder hands it over", labels)
        self.assertIn("first packet -> client present", labels)

    def test_says_when_nothing_joins_or_the_clocks_disagree(self):
        frames, sunshine, moonlight = make_run()
        for row in moonlight:
            row["host_us"] = str(int(row["host_us"]) + 7)  # a trace from another stream
        nothing = traces.analyse(traces.join(frames, START_US, sunshine, moonlight), len(frames), "s", "m")
        self.assertEqual(nothing.joined, 0)
        self.assertTrue(nothing.notes)
        # Sunshine on another clock: its captures land far from the test app's presents
        frames, sunshine, moonlight = make_run(capture_ms=7.5)
        late = traces.analyse(traces.join(frames, START_US, sunshine, moonlight), len(frames), "s", "m")
        self.assertEqual(late.notes, [])
        self.assertEqual(traces.join(frames, START_US + 60_000_000, sunshine, moonlight), [])

    def test_finds_and_picks_the_traces_that_fit(self):
        frames, sunshine, moonlight = make_run()
        _, other_sunshine, other_moonlight = make_run()
        for row in other_moonlight:
            row["host_us"] = str(int(row["host_us"]) + 5)  # another stream: no timestamps in common
        with tempfile.TemporaryDirectory() as d:
            write_csv(Path(d) / "sunshine-frames-1.csv", sunshine)
            write_csv(Path(d) / "Moonlight-pacing-1.csv", other_moonlight)
            write_csv(Path(d) / "Moonlight-pacing-2.csv", moonlight)
            sunshine_paths, moonlight_paths = traces.find([d, d])
            self.assertEqual(len(sunshine_paths), 1)
            self.assertEqual(len(moonlight_paths), 2)
            pipeline = traces.build(frames, START_US, sunshine_paths, moonlight_paths)
            self.assertEqual(pipeline.moonlight_file, "Moonlight-pacing-2.csv")
            self.assertEqual(pipeline.joined, 40)
            self.assertTrue(any("first packet" in line for line in traces.describe(pipeline)))
            # Given files win over the folder
            only = traces.find([d], moonlight=Path(d) / "Moonlight-pacing-1.csv")[1]
            self.assertEqual([p.name for p in only], ["Moonlight-pacing-1.csv"])
        self.assertIsNone(traces.build(frames, START_US, [], []))

    def test_works_with_only_sunshines_log(self):
        frames, sunshine, _ = make_run()
        pipeline = traces.analyse(traces.join(frames, START_US, sunshine, None), len(frames), "s.csv", None)
        labels = [s.label for s in pipeline.stages]
        self.assertEqual(pipeline.joined, 40)
        self.assertIn("host present -> capture", labels)
        self.assertIn("capture -> photon, to white", labels)
        self.assertFalse(any(s.source == "Moonlight" for s in pipeline.stages))
        self.assertEqual(pipeline.checks, {})
        self.assertIn("Pipeline:    40 of 40 frames followed through s.csv", traces.describe(pipeline)[0])

    def test_lines_up_moonlights_trace_without_sunshines(self):
        # The presents jitter a little, as real ones do, which gives the timing pattern to line up
        rng = np.random.default_rng(5)
        frames, _, moonlight = make_run(count=200)
        jitter = rng.normal(0, 150, len(frames))
        for f, j, m in zip(frames, jitter, moonlight):
            f["present_s"] += j / 1e6
            f["photon_s"] += j / 1e6
            m["host_us"] = str(int(float(m["host_us"]) + j + rng.normal(0, 60)))
        # Extra rows before and after the filmed stretch, as a whole stream has
        extra = [dict(moonlight[0], host_us=str(int(moonlight[0]["host_us"]) - 10_000 * k)) for k in range(1, 60)]
        joined = traces.join(frames, START_US, None, extra + moonlight)
        self.assertEqual(len(joined), 200)
        # Each frame got its own row
        self.assertTrue(all(j["moonlight"] is moonlight[i] for i, j in enumerate(joined)))
        pipeline = traces.analyse(joined, len(frames), None, "m.csv")
        self.assertIn("first packet -> client present", [s.label for s in pipeline.stages])
        self.assertNotIn("host present -> capture", [s.label for s in pipeline.stages])
        self.assertLess(pipeline.checks["moonlight_alignment_error_ms"], 0.2)
        self.assertEqual(pipeline.notes, [])

    def test_warns_when_moonlights_trace_lines_up_ambiguously(self):
        # Perfectly even presents give nothing to tell one frame from the next
        frames, _, moonlight = make_run(count=100)
        pipeline = traces.analyse(traces.join(frames, START_US, None, moonlight), len(frames), None, "m.csv")
        self.assertTrue(any("a frame off" in note for note in pipeline.notes))

    @staticmethod
    def write_report(with_traces):
        """The report for make_run's frames, with or without the traces; returns its HTML and its folder."""
        import types

        from camera_timing import pacing_stats
        from report import write_camera_report

        frames, sunshine, moonlight = make_run()
        pipeline = traces.analyse(traces.join(frames, START_US, sunshine, moonlight), len(frames), "s.csv", "m.csv") if with_traces else None
        fit = types.SimpleNamespace(windows=10, samples=100, min_rtt_ms=2.0, median_rtt_ms=4.0, residual_ms=0.1, drift_ppm=20.0)
        info = {"video": "run.mp4", "frames": 100, "fps": 240.0, "stream": (2560, 1440), "stream_fps": 97.0, "found": 100,
                "estimated": 0, "fit": fit, "flips_seen": 0, "flips_logged": 0, "device": "Test", "edge_difference": 0.0,
                "pipeline": pipeline}
        if with_traces:
            # Held by a limiter at 97 fps on a 100 Hz display
            from camera_timing import present_wait
            calls = np.arange(20) * 0.010309
            info["present_wait"] = present_wait(np.column_stack([np.arange(20), calls, calls, calls + 0.0102, np.zeros(20)]),
                                                {"refresh_hz": "100"})
        frame_rows = [{"frame": f["frame"], "photon_s": f["photon_s"], "latency": f["latency"], "rising": f["rising"]} for f in frames]
        pacing = pacing_stats([f["frame"] for f in frames], [f["photon_s"] for f in frames], [f["present_s"] for f in frames],
                              [f["frame"] for f in frames])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "run.report.html"
            write_camera_report(path, info, [], frame_rows, pacing, [],
                                {"flips": Path(d) / "a.csv", "frames": Path(d) / "b.csv", "regions": Path(d) / "c.png"})
            return path.read_text(encoding="utf-8"), d

    def test_report_shows_moonlights_capture_to_present(self):
        # The report's headline tiles with every stage present, Moonlight's capture to present included
        html, _ = self.write_report(True)
        self.assertIn("Capture &rarr; client present (Moonlight&#39;s capture to present)", html)
        self.assertIn("11.00 <small>ms</small>", html)
        self.assertIn("Client present &rarr; photon, to white / black", html)
        self.assertIn("Moonlight&#39;s &ldquo;capture to present&rdquo; is the middle of that", html)
        self.assertIn("<dt>Inside Present</dt><dd>median 10.20 ms per frame, presenting every 10.31 ms at 100 Hz "
                      "(a frame limiter: see the notes)</dd>", html)

    def test_report_without_traces_names_no_client_or_folder(self):
        # Most hosts and clients write no traces: nothing about Moonlight, and only file names, as reports get shared
        html, folder = self.write_report(False)
        self.assertNotIn("Moonlight", html)
        self.assertNotIn("Pipeline", html)
        self.assertNotIn("Inside Present", html)
        self.assertNotIn(str(Path(folder)), html)
        self.assertIn("Files: a.csv, b.csv, c.png</p>", html)

    def test_unknown_values_are_left_out(self):
        self.assertIsNone(traces.number({"x": "-1"}, "x"))
        self.assertIsNone(traces.number({"x": ""}, "x"))
        self.assertIsNone(traces.number({}, "x"))
        self.assertEqual(traces.number({"x": "12.5"}, "x"), 12.5)


if __name__ == "__main__":
    unittest.main()
