"""
Tests for comparing analysed runs. Run with: python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

import compare  # noqa: E402


def stats(median, sd=0.3):
    return {"n": 100, "median": median, "mean": median, "sd": sd, "p10": median - 0.4, "p90": median + 0.4}


def stage(label, median, source="Moonlight"):
    return {"label": label, "source": source, "n": 100, "median": median, "sd": 0.2, "p10": median - 0.2, "p90": median + 0.2}


# A stream with both logs: host present -> capture 0.7, capture -> client present 20.0 (client present at 20.7),
# photon at 40.5 going black and 37.5 going white
FULL_STAGES = [
    stage("host present -> capture", 0.7, "test app + Sunshine"),
    stage("capture -> encoded", 7.5, "Sunshine"),
    stage("capture -> sent", 7.6, "Sunshine"),
    stage("capture -> first packet", 2.2),
    stage("capture -> last packet", 7.9),
    stage("first packet -> last packet", 5.7),
    stage("last packet -> decoder hands it over", 2.0),
    stage("handed over -> GPU decode finished", 8.0),
    stage("ready -> client present (pacer hold)", 2.1),
    stage("first packet -> client present", 17.8),
    stage("capture -> client present (Moonlight's capture to present)", 20.0),
    stage("client present -> display (DXGI)", 0.4, "Moonlight (DXGI)"),
    stage("client present -> photon, to white", 16.8, "camera + Sunshine + Moonlight"),
    stage("client present -> photon, to black", 19.8, "camera + Sunshine + Moonlight"),
]


def make_folder(root, name, black, white, stages=None, sunshine=True, moonlight=True, missing=(), mismatch_sd=0.45,
                flips=(None, None), edge_difference=2.5):
    """A results folder as the analysis leaves it: summary, flips and frames."""
    folder = Path(root) / name
    folder.mkdir()
    pipeline = None
    if stages is not None:
        pipeline = {"sunshine_frames": "sunshine-frames-1.csv" if sunshine else None,
                    "moonlight_trace": "Moonlight-pacing-1.csv" if moonlight else None,
                    "camera_frames": 100, "joined": 100, "stages": stages, "checks": {}}
    frames = [f for f in range(1000, 1200) if f not in missing]
    flip_white, flip_black = flips
    summary = {
        "video": f"{name}.mp4",
        "flips": stats((flip_white or white + 1 + black + 1) / 2),
        "flips_to_white": stats(flip_white or white + 1),
        "flips_to_black": stats(flip_black or black + 1),
        "frames": stats((black + white) / 2, 1.5),
        "frames_to_white": stats(white),
        "frames_to_black": stats(black),
        "edge_difference_ms": edge_difference,
        "present_wait": {"inside_present": stats(8.3), "interval_ms": 8.55, "refresh_hz": 120, "kind": "limiter"},
        "pacing": {"mismatch": stats(0.0, mismatch_sd), "host_interval": stats(8.55), "client_interval": stats(8.55),
                   "uneven": 2, "pairs": 199, "frames_seen": len(frames), "frames_presented": 200,
                   "not_seen": len(missing)},
        "sync": {"min_rtt_ms": 2.1, "scatter_ms": 0.15, "drift_ppm": 10.0},
        "notes": [], "report": f"{name}.report.html", "pipeline": pipeline,
    }
    (folder / f"{name}.summary.json").write_text(json.dumps(summary), encoding="utf-8")
    lines = ["host_frame,present_us,photon_us,present_to_photon_ms,patch,edge,edge_offset_ms,host_interval_ms,client_interval_ms"]
    lines += [f"{f},0,0,{black},0,fall,0,," for f in frames]
    (folder / f"{name}.frames.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    flip_lines = ["seq,source,host_frame,present_us,photon_us,present_to_photon_ms,edge,edge_10_90_ms"]
    flip_lines += [f"{i},a,{1000 + i},0,0,{black},{'rise' if i % 2 else 'fall'},{17.0 if i % 2 else 11.0}" for i in range(10)]
    (folder / f"{name}.flips.csv").write_text("\n".join(flip_lines) + "\n", encoding="utf-8")
    return folder


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def findings(self, runs):
        return [f for r in runs for f in compare.run_checks(r)] + compare.pair_checks(runs)

    def test_local_against_a_fully_logged_stream(self):
        local = make_folder(self.root, "local", 24.0, 21.5)
        stream = make_folder(self.root, "stream", 40.5, 37.5, FULL_STAGES)
        runs = [compare.load_run(local, "Local", "local"), compare.load_run(stream, "Stream")]
        self.assertEqual(runs[1].kind, "stream")
        # The breakdown adds up to the measured medians, the shared steps first
        steps = compare.waterfall(runs[1])
        self.assertEqual([s[0] for s in steps][:2], ["Host present → capture", "Capture → first packet arrives"])
        self.assertAlmostEqual(steps[-2][2], 40.5)
        self.assertAlmostEqual(steps[-1][2], 37.5)
        self.assertAlmostEqual(dict((n, t) for n, _, t in steps)["Pacer hold → client present"], 20.7)
        self.assertAlmostEqual(compare.stream_part(runs[1]), 20.7)
        self.assertEqual(compare.waterfall(runs[0]), [])
        findings = self.findings(runs)
        text = " ".join(f.text for f in findings)
        self.assertIn("clock sync agrees with Sunshine", text)
        self.assertIn("in parts while it is still being encoded", text)
        self.assertIn("nothing queues on the client", text)
        # 8.0 ms of decode and draw in 8.55 ms frames
        self.assertTrue(any(f.level == "warn" and "GPU takes 8.00 ms" in f.text for f in findings))
        # 24.0 local against 19.8 through Moonlight: one finding covering both directions, pointing at the local present
        display = [f for f in findings if "display's own delay" in f.text]
        self.assertEqual(len(display), 1)
        self.assertEqual(display[0].level, "warn")
        self.assertIn("--no-noise", display[0].text)
        notes = compare.takeaways(runs)
        self.assertTrue(notes[0].startswith("Streaming changes host present → photon by +16.5 ms going black"))
        self.assertTrue(any("20.7 ms is the stream's own part" in n for n in notes))
        self.assertTrue(any("the stream adds no measurable unevenness" in n for n in notes))

    def test_writes_both_reports(self):
        local = make_folder(self.root, "local", 24.0, 21.5)
        stream = make_folder(self.root, "stream", 40.5, 37.5, FULL_STAGES)
        text, path = compare.compare([local, stream], ["Local", "Stream"], ["local", "auto"])
        self.assertEqual(path, self.root / "compare_local_vs_stream.html")
        page = path.read_text(encoding="utf-8")
        self.assertIn("<h1>Local vs Stream</h1>", page)
        self.assertIn("Host present &rarr; photon, by stage", page)
        self.assertIn("Stream: where the time goes", page)
        # The console text is plain ASCII, for Windows consoles
        text.encode("ascii")
        self.assertIn("Client present -> photon, to black", text)

    def test_two_streams_with_different_logs(self):
        # One with only Sunshine's log, one with only Moonlight's: neither places the client's stages on the host's
        # timeline, so the breakdown is as far as each gets, and nothing fails for want of a stage
        sunshine_only = [s for s in FULL_STAGES if s["source"] in ("Sunshine", "test app + Sunshine")]
        moonlight_only = [s for s in FULL_STAGES if s["source"].startswith("Moonlight") and "capture" not in s["label"]]
        a = make_folder(self.root, "a", 40.0, 37.0, sunshine_only, moonlight=False)
        b = make_folder(self.root, "b", 45.0, 42.0, moonlight_only, sunshine=False)
        runs = [compare.load_run(a), compare.load_run(b)]
        self.assertEqual([r.kind for r in runs], ["stream", "stream"])
        steps = compare.waterfall(runs[0])
        self.assertEqual([s[0] for s in steps], ["Host present → capture",
                                                 "Capture → photon (network, client and display), to black",
                                                 "Capture → photon (network, client and display), to white"])
        self.assertEqual(compare.waterfall(runs[1]), [])
        self.assertEqual(compare.segments(runs[1], "black"), [("Host present → photon", compare.WHOLE, 45.0)])
        notes = compare.takeaways(runs)
        self.assertTrue(notes[0].startswith("b against a: host present → photon +5.0 ms going black"))
        self.assertFalse(any("display's own delay" in f.text for f in self.findings(runs)))
        compare.compare([a, b])

    def test_two_streams_both_logged_show_what_changed(self):
        slower = [dict(s, median=s["median"] + 3.0) if s["label"] == "handed over -> GPU decode finished" else s
                  for s in FULL_STAGES]
        a = make_folder(self.root, "a", 40.5, 37.5, FULL_STAGES)
        b = make_folder(self.root, "b", 43.5, 40.5, slower, mismatch_sd=0.9)
        runs = [compare.load_run(a), compare.load_run(b)]
        notes = compare.takeaways(runs)
        self.assertTrue(any("Biggest stage changes" in n and "handed over → GPU decode finished +3.00 ms" in n for n in notes))
        self.assertTrue(any(n.startswith("b paces less evenly") for n in notes))
        # The same display through Moonlight in both
        self.assertTrue(any(f.level == "ok" and "display's own delay agrees" in f.text for f in self.findings(runs)))

    def test_runs_without_logs(self):
        a = make_folder(self.root, "a", 24.0, 21.5)
        b = make_folder(self.root, "b", 40.0, 37.0)
        runs = [compare.load_run(a), compare.load_run(b)]
        self.assertEqual([r.kind for r in runs], ["unknown", "unknown"])
        self.assertEqual(self.findings(runs), [])
        compare.compare([a, b])

    def test_moonlights_clock_sync_against_sunshine(self):
        # The last packet "arriving" 3 ms before Sunshine sent it can't be
        early = [dict(s, median=4.6) if s["label"] == "capture -> last packet" else s for s in FULL_STAGES]
        run = compare.load_run(make_folder(self.root, "a", 40.5, 37.5, early))
        self.assertTrue(any(f.level == "warn" and "clock sync is off" in f.text for f in compare.run_checks(run)))
        # Older summaries without capture -> last packet: first packet plus first -> last
        older = [s for s in FULL_STAGES if s["label"] != "capture -> last packet"]
        run = compare.load_run(make_folder(self.root, "b", 40.5, 37.5, older))
        self.assertTrue(any("last packet arrived 0.30 ms after" in f.text for f in compare.run_checks(run)))

    def test_frames_not_seen(self):
        # Multiples of 4 only: misread patches, not drops
        run = compare.load_run(make_folder(self.root, "a", 40.0, 37.0, missing=(1004, 1016, 1024, 1060, 1100)))
        text = " ".join(f.text for f in compare.run_checks(run))
        self.assertIn("all multiples of 4", text)
        self.assertIn("misread patches", text)
        # Odd ones among them: as dropped frames would fall
        run = compare.load_run(make_folder(self.root, "b", 40.0, 37.0, missing=(1003, 1016, 1061)))
        self.assertIn("likely dropped", " ".join(f.text for f in compare.run_checks(run)))
        self.assertEqual(compare.changed_patch(2384), 3)
        self.assertEqual(compare.changed_patch(2385), 7)
        self.assertEqual(compare.changed_patch(2304), 0)

    def test_trigger_and_patches_disagreeing_on_the_slower_edge(self):
        # Patches slower going black, the trigger slower going white
        run = compare.load_run(make_folder(self.root, "a", 24.0, 21.5, flips=(26.1, 24.6)))
        text = " ".join(f.text for f in compare.run_checks(run))
        self.assertIn("dimming large bright areas", text)
        self.assertIn("17.0 ms from 10% to 90% going white", text)

    def test_needs_analysed_folders(self):
        (self.root / "empty").mkdir()
        with self.assertRaises(SystemExit):
            compare.load_run(self.root / "empty")
        with self.assertRaises(SystemExit):
            compare.main([str(self.root / "empty")])


if __name__ == "__main__":
    unittest.main()
