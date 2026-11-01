"""
Tests for the Latency Cam analysis. Run with: python -m unittest discover -s tests
"""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analyzer"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera_timing import (  # noqa: E402
    assign_frames,
    bit_crossings,
    changed_bit,
    decode_frame_changes,
    edge_offsets,
    fit_clock,
    gray_encode,
    load_camera_timing,
    load_host_log,
    match_trigger_flips,
    pacing_stats,
    patch_turned_white,
    refine_crossing,
)


def timing_file(directory, **overrides):
    """A minimal timing file: three frames 1/240 s apart, with the encoder's clock 1 s behind the capture clock."""
    starts = [5_000_000_000 + k * 4_166_667 for k in range(3)]
    data = {
        "format": "ml-latency-camera", "width": 1920, "height": 1080, "fps": 240, "timestamp_source": "REALTIME",
        "clocks_at_start": [2_000_000_000, 1_000_000_000, 400], "clocks_at_stop": [3_000_000_000, 2_000_000_000, 400],
        "video_pts_us": [(s - 1_000_000_000) // 1000 for s in reversed(starts)],
        "active_array": [0, 0, 4080, 3072],
        "capture_fields": ["sensor_timestamp_ns", "exposure_ns", "rolling_shutter_skew_ns", "frame_duration_ns",
                           "crop_left", "crop_top", "crop_width", "crop_height"],
        "captures": [[s, 4_000_000, 4_542_720, 33_000_000, 0, 0, 4080, 3072] for s in starts],
        "sync": {"qpc_frequency": 10_000_000, "samples": []},
    }
    data.update(overrides)
    path = Path(directory) / "v.camera.json"
    path.write_text(json.dumps(data))
    return path, starts


class CameraTimingTest(unittest.TestCase):
    def test_lines_capture_results_up_with_frames(self):
        with tempfile.TemporaryDirectory() as d:
            path, starts = timing_file(d)
            timing = load_camera_timing(path)
        np.testing.assert_allclose(timing.starts_ns, starts, atol=1000)
        self.assertEqual(timing.unmatched, 0)
        # A 16:9 video from a 4:3 crop covers the middle 3/4 of the rows
        self.assertAlmostEqual(timing.row_offset[0], (3072 - 4080 * 9 / 16) / 2 / 3072)
        self.assertAlmostEqual(timing.row_span[0], 4080 * 9 / 16 / 3072)

    def test_row_time_follows_the_rolling_shutter(self):
        with tempfile.TemporaryDirectory() as d:
            path, starts = timing_file(d)
            timing = load_camera_timing(path)
        top = timing.row_time_ns(1, 0)
        bottom = timing.row_time_ns(1, 1080)
        self.assertAlmostEqual(top, starts[1] + 4_542_720 * timing.row_offset[1] + 2_000_000, delta=1)
        self.assertAlmostEqual(bottom - top, 4_542_720 * timing.row_span[1], delta=1)
        # Halfway between frames 1 and 2
        self.assertAlmostEqual(timing.row_time_ns(1.5, 0) - top, (starts[2] - starts[1]) / 2, delta=1)

    def test_rejects_files_it_cannot_use(self):
        with tempfile.TemporaryDirectory() as d:
            path, _ = timing_file(d, timestamp_source="UNKNOWN")
            with self.assertRaises(ValueError):
                load_camera_timing(path)
            path, _ = timing_file(d, format="other")
            with self.assertRaises(ValueError):
                load_camera_timing(path)

    def test_assumes_full_rows_without_a_crop(self):
        with tempfile.TemporaryDirectory() as d:
            path, starts = timing_file(d, active_array=None)
            timing = load_camera_timing(path)
        self.assertEqual(timing.row_offset[0], 0)
        self.assertEqual(timing.row_span[0], 1)


class ClockFitTest(unittest.TestCase):
    def test_recovers_offset_and_drift_despite_queuing(self):
        rng = np.random.default_rng(3)
        frequency = 10_000_000
        drift = 20e-6  # the phone runs 20 ppm fast
        samples = []
        for k in range(400):
            host = 1000.0 + k * 0.05
            up, down = 0.001 + rng.exponential(0.0005), 0.001 + rng.exponential(0.0005)
            phone = lambda h: h * (1 + drift) * 1e9 + 7e12
            samples.append([phone(host), (host + up) * frequency, (host + up + 1e-5) * frequency, phone(host + up + 1e-5 + down)])
        fit = fit_clock(samples, frequency)
        for host in (1001.0, 1010.0, 1019.0):
            phone = host * (1 + drift) * 1e9 + 7e12
            self.assertAlmostEqual(float(fit.host_seconds(phone)), host, delta=0.0002)
        self.assertAlmostEqual(fit.drift_ppm, 20, delta=3)
        self.assertLess(fit.min_rtt_ms, 2.5)

    def test_needs_samples(self):
        with self.assertRaises(ValueError):
            fit_clock([], 10_000_000)


class HostLogTest(unittest.TestCase):
    def test_reads_both_logs(self):
        header = "# latency-test\n# start_qpc=100 qpc_frequency=10000000\n"
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "run.csv").write_text(header + "seq,source,detail,input_us,render_us,present_call_us,present_return_us,frame,still_queued\n"
                                             "1,a,0,10.0,11.0,12.0,13.0,5,0\n")
            (Path(d) / "run.frames.csv").write_text(header + "frame,render_us,present_call_us,present_return_us,input_seq\n"
                                                    "5,11.0,12.0,13.0,1\n6,20.0,21.0,22.0,1\n")
            log = load_host_log(Path(d) / "run.frames.csv")
        self.assertEqual(log.start_qpc, 100)
        self.assertEqual(log.flip_sources, ["a"])
        np.testing.assert_allclose(log.flips[0], [1, 0, 12e-6, 5])
        np.testing.assert_allclose(log.frames[:, 0], [5, 6])
        self.assertAlmostEqual(log.frames[1, 2], 21e-6)

    def test_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.csv").write_text("a,b\n1,2\n")
            with self.assertRaises(ValueError):
                load_host_log(Path(d) / "x.csv")


class MatchingTest(unittest.TestCase):
    def test_matches_flips_by_colour_and_time(self):
        flips = np.array([[1, 0, 1.00, 10], [2, 0, 1.30, 40], [3, 0, 1.55, 70]])
        # Seen 50 ms after each; odd sequence numbers turn the trigger white
        self.assertEqual(match_trigger_flips([1.05, 1.35, 1.60], [True, False, True], flips), [0, 1, 2])
        # A flip seen in the wrong colour has no match
        self.assertEqual(match_trigger_flips([1.05], [False], flips), [None])
        # Each host flip is used once
        self.assertEqual(match_trigger_flips([1.05, 1.06], [True, True], flips), [0, None])

    def test_changed_bit_follows_the_gray_code(self):
        for frame in range(1, 600):
            diff = gray_encode(frame & 255) ^ gray_encode((frame - 1) & 255)
            self.assertEqual(1 << (7 - changed_bit(frame)), diff)

    def test_assigns_the_latest_frame_with_that_number(self):
        frames = np.array([[n, 0, n / 120, 0, 0] for n in range(600)], dtype=np.float64)
        # Frame 300 shows as 300 & 255 = 44, seen 40 ms after it was presented
        self.assertEqual(assign_frames([(300 / 120 + 0.04, 44)], frames), [300])
        self.assertEqual(assign_frames([(0.0, 44)], frames), [None])


class FrameDecodingTest(unittest.TestCase):
    def test_finds_crossings_with_hysteresis(self):
        values = np.array([0, 0, 0.2, 0.45, 0.55, 0.8, 1, 1, 0.6, 0.4, 0.1, 0])
        crossings = bit_crossings(values)
        self.assertEqual([r for _, r in crossings], [True, False])
        self.assertAlmostEqual(crossings[0][0], 3.5, delta=0.1)
        self.assertAlmostEqual(crossings[1][0], 8.5, delta=0.1)
        # Wobbling around 0.5 without reaching the far threshold isn't a change
        self.assertEqual(bit_crossings(np.array([0, 0.45, 0.55, 0.45, 0.55, 0])), [])

    def test_refine_crossing_follows_a_curved_edge(self):
        # An exponential rise sampled every frame: the cubic lands closer to the true crossing than a straight line
        t = np.arange(10.0)
        values = np.where(t >= 3, 1 - np.exp(-(t - 3) / 1.5), 0.0)
        true = 3 + 1.5 * np.log(2)
        k = int(np.floor(true))
        linear = k + (0.5 - values[k]) / (values[k + 1] - values[k])
        self.assertLess(abs(refine_crossing(values, k, 0.5) - true), abs(linear - true))

    def test_decodes_each_frames_arrival(self):
        # Two camera frames per host frame, with each change taking one camera frame
        camera_frames = 60
        bits = []
        for b in range(8):
            values = []
            for k in range(camera_frames):
                frame, phase = 100 + k // 2, k % 2
                now = (gray_encode(frame & 255) >> (7 - b)) & 1
                before = (gray_encode((frame - 1) & 255) >> (7 - b)) & 1
                values.append(0.5 * (now + before) if phase == 0 else float(now))
            bits.append(np.array(values))
        arrivals = decode_frame_changes(bits)
        values = [v for _, v, _ in arrivals]
        self.assertEqual(values, list(range(101, 130)))
        # Each frame arrives on the camera frame where its patch is halfway through changing
        self.assertTrue(all(abs(p - 2 * (v - 100)) < 0.01 for p, v, _ in arrivals))

    def test_pacing_compares_intervals(self):
        numbers = [10, 11, 12, 14]
        present = [0.0, 0.010, 0.020, 0.040]
        photon = [0.050, 0.060, 0.075, 0.090]
        pacing = pacing_stats(numbers, photon, present, list(range(0, 20)))
        np.testing.assert_allclose(pacing.host_intervals_ms, [10, 10], atol=1e-9)
        np.testing.assert_allclose(pacing.client_intervals_ms, [10, 15], atol=1e-9)
        np.testing.assert_allclose(pacing.mismatch_ms, [0, 5], atol=1e-9)
        self.assertEqual(pacing.uneven, 1)
        self.assertEqual(pacing.frames_presented, 5)
        self.assertEqual(pacing.not_seen, 1)


class EdgeTest(unittest.TestCase):
    def test_patch_direction_follows_the_gray_code(self):
        for frame in range(1, 600):
            bit = changed_bit(frame)
            self.assertEqual(patch_turned_white(frame, bit), bool((gray_encode(frame & 255) >> (7 - bit)) & 1))

    def test_offsets_per_patch_and_direction(self):
        # Going to black reads 3 ms later; patch 7 reads 1 ms earlier on top; patch 2 has too few for its own offset
        patches, rising, latencies = [], [], []
        for k in range(200):
            patch = 7 if k % 2 else 6
            up = k % 4 < 2
            patches.append(patch)
            rising.append(up)
            latencies.append(25 + (0 if up else 3) - (1 if patch == 7 else 0))
        patches += [2, 2]
        rising += [True, False]
        latencies += [99, 99]
        offsets, by_direction = edge_offsets(patches, rising, latencies)
        self.assertAlmostEqual(by_direction[False] - by_direction[True], 3, delta=0.6)
        self.assertAlmostEqual(offsets[(6, False)] - offsets[(6, True)], 3)
        self.assertAlmostEqual(offsets[(6, True)] - offsets[(7, True)], 1)
        self.assertEqual(offsets[(2, True)], by_direction[True])
        self.assertEqual(edge_offsets([], [], []), ({}, {True: 0.0, False: 0.0}))


def analyse_synthetic(test, directory, **recording):
    """Make a synthetic recording, analyse it, and return the generator's result with the analyzer's CSVs."""
    from synthetic_camera import make_recording

    made = make_recording(directory, seconds=1.6, camera=(960, 540), **recording)
    result = subprocess.run(
        [sys.executable, str(ROOT / "analyzer" / "analyze_video.py"), str(Path(directory) / "video.mp4"),
         "--stream", "1280x720", "--stream-fps", "120", "--host-log", str(Path(directory) / "host.csv"), "--report"],
        capture_output=True, text=True,
    )
    test.assertEqual(result.returncode, 0, result.stderr)
    frames = list(csv.DictReader((Path(directory) / "video.frames.csv").read_text().splitlines()))
    flips = list(csv.DictReader((Path(directory) / "video.flips.csv").read_text().splitlines()))
    summary = json.loads((Path(directory) / "video.summary.json").read_text())
    test.assertTrue((Path(directory) / "video.report.html").is_file())
    return made, frames, flips, summary


def truth_of(made, row):
    """The true present to halfway time of a frame or flip row, for the direction its patch or trigger changed."""
    return (made["truth_ms"] if row["edge"] == "rise" else made["truth_fall_ms"])[int(row["host_frame"])]


class EndToEndTest(unittest.TestCase):
    """Analyse synthetic Latency Cam recordings with known latencies."""

    def test_recovers_every_frames_latency(self):
        with tempfile.TemporaryDirectory() as d:
            made, frames, flips, _ = analyse_synthetic(self, d)

        errors = np.array([float(r["present_to_photon_ms"]) - truth_of(made, r) for r in frames])
        self.assertGreater(len(frames), 150)
        # Averaging over the exposure and interpolating between camera frames read a realistic edge ~0.4 ms late
        self.assertLess(abs(np.median(errors)), 0.7)
        self.assertLess(np.percentile(np.abs(errors), 95), 1.2)
        flip_errors = [float(r["present_to_photon_ms"]) - truth_of(made, r) for r in flips]
        self.assertGreater(len(flips), 3)
        self.assertLess(max(abs(e) for e in flip_errors), 1.2)

    def test_slow_falls_do_not_look_like_uneven_pacing(self):
        # A display that takes twice as long going to black, with frames arriving exactly as evenly as presented
        with tempfile.TemporaryDirectory() as d:
            made, frames, flips, summary = analyse_synthetic(self, d, tau_ms=3.0, tau_fall_ms=6.0, latency_jitter_ms=0)

        # Each direction is still measured against its own truth
        errors = np.array([float(r["present_to_photon_ms"]) - truth_of(made, r) for r in frames])
        self.assertLess(np.percentile(np.abs(errors), 95), 1.2)
        self.assertAlmostEqual(summary["edge_difference_ms"], 3 * np.log(2), delta=0.6)
        self.assertGreater(summary["frames_to_black"]["median"], summary["frames_to_white"]["median"] + 1.4)
        # Corrected for the edges, pacing is about as even as the host's own presents (0.2 ms jitter each)
        self.assertLess(summary["pacing"]["mismatch"]["sd"], 0.6)
        self.assertEqual(summary["pacing"]["uneven"], 0)


if __name__ == "__main__":
    unittest.main()
