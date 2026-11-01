"""
Analysis of a video recorded with Latency Cam, whose frames are timed on the host's clock: for every trigger flip and
every host frame, the time from the host presenting it to it reaching the client display, and how evenly the client
showed the host's frames.
"""
import csv
import json
import os
import sys
from pathlib import Path

import numpy as np

from analyze_video import Canvas, compute_layout, describe, draw_regions, find_flips, read_input_seq, read_video, region_series
from camera_timing import (
    assign_frames,
    decode_frame_changes,
    fit_clock,
    load_camera_timing,
    load_host_log,
    match_trigger_flips,
    normalize,
    pacing_stats,
    trigger_crossing,
)
from report import write_camera_report


def default_host_log():
    return Path(os.environ.get("TEMP", ".")) / "sunshine_latency_test.csv"


def camera_row(transform, canvas, rect):
    """The camera row (full resolution) where the middle of a pattern rectangle was seen."""
    u, v = canvas.to_canvas(*rect.center)
    x, y, w = transform @ np.array([u, v, 1.0])
    return y / w


class PhotonClock:
    """Turns a fractional camera frame and a pattern rectangle into host seconds since the host log's start."""

    def __init__(self, timing, fit, host, transforms, canvas, row_scale):
        self.timing = timing
        self.fit = fit
        self.zero = host.start_qpc / host.frequency
        self.canvas = canvas
        self.row_scale = row_scale
        # Frames before the fiducials were found borrow the first transform
        first = next((t for t in transforms if t is not None), None)
        self.transforms = []
        last = first
        for t in transforms:
            last = t if t is not None else last
            self.transforms.append(last)

    def __call__(self, position, rect):
        k = int(np.clip(np.floor(position), 0, len(self.transforms) - 1))
        row = camera_row(self.transforms[k], self.canvas, rect) * self.row_scale
        row = float(np.clip(row, 0, self.timing.height - 1))
        phone_ns = self.timing.row_time_ns(min(position, len(self.timing.starts_ns) - 1), row)
        return float(self.fit.host_seconds(phone_ns)) - self.zero


def run(args, timing_path):
    timing = load_camera_timing(timing_path)
    host_log_path = args.host_log or default_host_log()
    if not Path(host_log_path).is_file():
        sys.exit(f"No host log at {host_log_path}. Pass --host-log with the CSV latency-test.exe wrote during the recording.")
    host = load_host_log(host_log_path)
    sync = timing.info["sync"]
    if int(sync["qpc_frequency"] or 0) not in (0, host.frequency):
        sys.exit("The phone synced with a host whose clock runs at another rate than the host log's. Is it the same host?")
    fit = fit_clock(sync["samples"], sync["qpc_frequency"])

    width, height = args.stream
    layout = compute_layout(width, height)
    canvas = Canvas(width, height, layout)
    out_dir = args.out or args.video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / args.video.stem

    print(f"Reading {args.video.name}, timed by {Path(timing_path).name}", file=sys.stderr)
    stack, valid, found, three, reference, transforms, size = read_video(args.video, canvas, layout, args.max_frames)
    frames = stack.shape[0]
    if found == 0:
        sys.exit("The corner bullseyes were not found in any frame. Is the whole pattern in shot?")
    if frames > len(timing.starts_ns):
        print(f"warning: the video has {frames} frames but the timing file {len(timing.starts_ns)}; using the first "
              f"{len(timing.starts_ns)}", file=sys.stderr)
        frames = len(timing.starts_ns)
        stack, valid, transforms = stack[:frames], valid[:frames], transforms[:frames]
    clock = PhotonClock(timing, fit, host, transforms, canvas, timing.height / size[1])

    # Host present to photon for each trigger flip
    trigger_strip = layout["trigger"].inset(0.6, 0.25)
    trigger = region_series(stack, valid, canvas, trigger_strip)
    flips, _ = find_flips(trigger, 1.0, 20)  # times in camera frames
    flip_times = [clock(trigger_crossing(trigger, f.time), trigger_strip) for f in flips]
    matches = match_trigger_flips(flip_times, [f.rising for f in flips], host.flips)
    flip_rows = []
    seq_mismatch = 0
    for flip, t, m in zip(flips, flip_times, matches):
        if m is None:
            continue
        seq, _, present, frame = host.flips[m]
        shown = read_input_seq(stack, canvas, layout, min(flip.frame + 6, frames - 1))
        seq_mismatch += shown != int(seq) % 16
        flip_rows.append({
            "seq": int(seq),
            "source": host.flip_sources[m],
            "frame": int(frame),
            "present_s": present,
            "photon_s": t,
            "latency": (t - present) * 1000,
            "rising": flip.rising,
            "edge_ms": flip.ramp * 1000 / timing.fps,
        })

    # Host present to photon for every frame, from the frame number patches
    bit_values = [normalize(region_series(stack, valid, canvas, bit.inset(0.5, 0.5))) for bit in layout["frame_bits"]]
    arrivals = decode_frame_changes(bit_values)
    times = [clock(position, layout["frame_bits"][bit]) for position, _, bit in arrivals]
    rows = assign_frames([(t, value) for t, (_, value, _) in zip(times, arrivals)], host.frames)
    seen = {}
    for t, row in zip(times, rows):
        if row is None:
            continue
        number = int(host.frames[row, 0])
        if number not in seen or t < seen[number][0]:
            seen[number] = (t, row)
    frame_rows = []
    for number in sorted(seen):
        t, row = seen[number]
        present = host.frames[row, 2]
        frame_rows.append({"frame": number, "present_s": present, "photon_s": t, "latency": (t - present) * 1000})
    pacing = pacing_stats(
        [r["frame"] for r in frame_rows], [r["photon_s"] for r in frame_rows], [r["present_s"] for r in frame_rows],
        host.frames[:, 0].astype(np.int64),
    )

    # Outputs
    flips_csv = stem.with_name(stem.name + ".flips.csv")
    with open(flips_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "source", "host_frame", "present_us", "photon_us", "present_to_photon_ms", "edge", "edge_10_90_ms"])
        for r in flip_rows:
            writer.writerow([r["seq"], r["source"], r["frame"], f"{r['present_s'] * 1e6:.1f}", f"{r['photon_s'] * 1e6:.1f}",
                             f"{r['latency']:.3f}", "rise" if r["rising"] else "fall", f"{r['edge_ms']:.2f}"])
    frames_csv = stem.with_name(stem.name + ".frames.csv")
    by_frame = {r["frame"]: r for r in frame_rows}
    with open(frames_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["host_frame", "present_us", "photon_us", "present_to_photon_ms", "host_interval_ms", "client_interval_ms"])
        for r in frame_rows:
            previous = by_frame.get(r["frame"] - 1)
            host_interval = client_interval = ""
            if previous:
                host_interval = f"{(r['present_s'] - previous['present_s']) * 1000:.3f}"
                client_interval = f"{(r['photon_s'] - previous['photon_s']) * 1000:.3f}"
            writer.writerow([r["frame"], f"{r['present_s'] * 1e6:.1f}", f"{r['photon_s'] * 1e6:.1f}", f"{r['latency']:.3f}",
                             host_interval, client_interval])

    regions_path = stem.with_name(stem.name + ".regions.png")
    if reference is not None:
        draw_regions(regions_path, reference, canvas, [
            ("trigger", trigger_strip, (0, 0, 255)),
            *[(f"f{i}", b.inset(0.5, 0.5), (255, 0, 0)) for i, b in enumerate(layout["frame_bits"])],
        ])

    flip_latencies = [r["latency"] for r in flip_rows]
    frame_latencies = [r["latency"] for r in frame_rows]
    print()
    print(f"Video:       {frames} frames at {timing.fps:g} fps = {frames / timing.fps:.2f} s; stream {width}x{height}")
    print(f"Bullseyes:   found in {found}/{frames} frames" + (f", {three} of them with one hidden and estimated" if three else ""))
    print(f"Clock sync:  {fit.windows} of {fit.samples} exchanges used; fastest round trip {fit.min_rtt_ms:.2f} ms "
          f"(median {fit.median_rtt_ms:.2f}); scatter {fit.residual_ms:.3f} ms; phone clock {fit.drift_ppm:+.1f} ppm against the host's")
    print(f"Flips:       {len(flips)} seen, {len(flip_rows)} matched to the host log ({len(host.flips)} logged)")
    print()
    print("Host present to photon, ms:")
    print(f"  trigger flips  {describe(flip_latencies)}")
    print(f"  every frame    {describe(frame_latencies)}")
    print()
    print(f"Pacing:      {pacing.frames_seen} frames seen of {pacing.frames_presented} presented in that time "
          f"({pacing.not_seen} not seen)")
    print(f"  host interval     {describe(pacing.host_intervals_ms)}")
    print(f"  client interval   {describe(pacing.client_intervals_ms)}")
    print(f"  client - host     {describe(pacing.mismatch_ms)}")
    print(f"  uneven            {pacing.uneven} of {pacing.mismatch_ms.size} intervals off by more than a quarter frame")

    notes = []
    if fit.min_rtt_ms > 3:
        notes.append(f"The fastest clock sync round trip was {fit.min_rtt_ms:.1f} ms. The sync assumes the request and "
                     "reply took equally long, so every time here could be off by up to half that, the same way for "
                     "all of them. A wired link or a quieter network brings it down.")
    if fit.residual_ms > 0.3:
        notes.append(f"The clock sync scattered by {fit.residual_ms:.2f} ms around its fit, so single measurements "
                     "carry that much noise.")
    if timing.unmatched:
        notes.append(f"{timing.unmatched} video frames had no capture result and were timed from their neighbours.")
    if seq_mismatch:
        notes.append(f"For {seq_mismatch} flips the input number on screen didn't match the host log's. Check that "
                     "the host log is from the run that was filmed.")
    if flips and not flip_rows:
        notes.append("No trigger flip matched the host log. Check that the host log is from the run that was filmed "
                     "and that the phone synced with the same host.")
    if pacing.frames_presented and pacing.not_seen > pacing.frames_presented * 0.2:
        notes.append(f"{pacing.not_seen} of {pacing.frames_presented} host frames weren't seen. Some may have been "
                     "dropped, but more often the frame number patches were too small or blurred to read: frame "
                     "the display larger.")
    for note in notes:
        print(f"Note: {note}")

    def stats(values):
        values = np.asarray(values, dtype=np.float64)
        if values.size == 0:
            return None
        return {"n": int(values.size), "median": round(float(np.median(values)), 3), "mean": round(float(values.mean()), 3),
                "sd": round(float(values.std()), 3), "p10": round(float(np.percentile(values, 10)), 3),
                "p90": round(float(np.percentile(values, 90)), 3)}

    summary_path = stem.with_name(stem.name + ".summary.json")
    summary_path.write_text(json.dumps({
        "video": args.video.name,
        "flips": stats(flip_latencies),
        "frames": stats(frame_latencies),
        "pacing": {
            "mismatch": stats(pacing.mismatch_ms),
            "host_interval": stats(pacing.host_intervals_ms),
            "client_interval": stats(pacing.client_intervals_ms),
            "uneven": pacing.uneven,
            "pairs": int(pacing.mismatch_ms.size),
            "frames_seen": pacing.frames_seen,
            "frames_presented": pacing.frames_presented,
            "not_seen": pacing.not_seen,
        },
        "sync": {"min_rtt_ms": round(fit.min_rtt_ms, 3), "scatter_ms": round(fit.residual_ms, 4), "drift_ppm": round(fit.drift_ppm, 2)},
        "notes": notes,
        "report": stem.name + ".report.html" if args.report else None,
    }, indent=1))

    print()
    print(f"Wrote {flips_csv.name}, {frames_csv.name}, {summary_path.name} and {regions_path.name} in {out_dir}")
    if args.report:
        report_path = stem.with_name(stem.name + ".report.html")
        info = {
            "video": args.video,
            "frames": frames,
            "fps": timing.fps,
            "stream": (width, height),
            "stream_fps": args.stream_fps,
            "found": found,
            "estimated": three,
            "fit": fit,
            "flips_seen": len(flips),
            "flips_logged": len(host.flips),
            "device": timing.info.get("device", ""),
        }
        write_camera_report(report_path, info, flip_rows, frame_rows, pacing, notes,
                            {"flips": flips_csv, "frames": frames_csv, "regions": regions_path})
        print(f"Wrote {report_path}")
