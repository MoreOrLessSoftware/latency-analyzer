"""
Compare analysed runs side by side: latency, pacing, where the time went in each, and checks that the runs agree.

Works from what the analysis already wrote to each results folder (NAME.summary.json, NAME.flips.csv and
NAME.frames.csv), so nothing is analysed again. Each run is one of:

- local: the test app ran on the client itself, filmed on the client's own display, with no stream. Its host
  present -> photon is then the display's own delay (plus whatever the client's GPU takes to draw the pattern).
- stream: the test app ran on the host and was streamed to the client. A run with Sunshine's or Moonlight's logs is
  taken to be one.
- unknown: neither said nor shown by the logs.

Either run may have Sunshine's frame log, Moonlight's pacing trace, both or neither: each part of the comparison uses
what the runs have and leaves out what they don't.

Usage:
  latency-analyzer compare RUN RUN [RUN...] [--kinds local stream] [--labels "Client only" "Streaming"] [--out FILE]
"""
import argparse
import csv
import html
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

KINDS = ("local", "stream", "unknown")

# The stream's milestones, in order, as stage labels from the analysis (see traces.py)
HOST_TO_CAPTURE = "host present -> capture"
CAPTURE_TO_ENCODED = "capture -> encoded"
CAPTURE_TO_SENT = "capture -> sent"
CAPTURE_TO_FIRST = "capture -> first packet"
CAPTURE_TO_LAST = "capture -> last packet"
FIRST_TO_LAST = "first packet -> last packet"
LAST_TO_HANDOVER = "last packet -> decoder hands it over"
HANDOVER_TO_GPU = "handed over -> GPU decode finished"
FIRST_TO_CLIENT_PRESENT = "first packet -> client present"
CAPTURE_TO_CLIENT_PRESENT = "capture -> client present (Moonlight's capture to present)"
CLIENT_PRESENT_TO_DISPLAY = "client present -> display (DXGI)"

# Phases of the path, for the bars: what colour each segment takes
HOST, CLIENT, DISPLAY, WHOLE = "host", "client", "display", "whole"
PHASE_NAMES = {HOST: "Host and network: capture, encode, send", CLIENT: "Client: receive, decode, pace",
               DISPLAY: "Client display: present → photon", WHOLE: "Not broken down"}


@dataclass
class Run:
    folder: Path
    label: str
    kind: str
    summary: dict
    flips: list  # rows of NAME.flips.csv
    frames: list  # host frame numbers seen, from NAME.frames.csv
    stages: dict = field(default_factory=dict)  # stage label -> its stats from the summary

    @property
    def sunshine(self):
        return bool((self.summary.get("pipeline") or {}).get("sunshine_frames"))

    @property
    def moonlight(self):
        return bool((self.summary.get("pipeline") or {}).get("moonlight_trace"))

    @property
    def noise(self):
        """The background noise's amplitude while filming (0 for none), or None when it changed or isn't known."""
        return self.summary.get("noise")

    def describe(self):
        """What the run was, for the report's heading: its kind, noise and logs."""
        parts = [self.kind]
        if self.noise is not None:
            parts.append(f"noise {self.noise:g}" if self.noise > 0 else "no noise")
        logs = [n for n, has in (("Sunshine", self.sunshine), ("Moonlight", self.moonlight)) if has]
        if logs:
            parts.append(f"{' and '.join(logs)} logs")
        return ", ".join(parts)

    def median(self, key):
        """A median from the summary ('frames_to_black') or a stage ('host present -> capture'), or None."""
        if key in self.stages:
            return self.stages[key]["median"]
        value = self.summary.get(key)
        return value["median"] if isinstance(value, dict) and "median" in value else None

    def frame_interval(self):
        wait = self.summary.get("present_wait") or {}
        if wait.get("interval_ms"):
            return wait["interval_ms"]
        host = (self.summary.get("pacing") or {}).get("host_interval") or {}
        return host.get("median")


def read_rows(path):
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(line for line in f if line.strip() and not line.startswith("#")))


def load_run(folder, label=None, kind="auto"):
    """A results folder that has been analysed; exits with a message when it hasn't."""
    folder = Path(folder)
    if not folder.is_dir():
        sys.exit(f"No such results folder: {folder}")
    summaries = [folder / f"{folder.name}.summary.json"] + sorted(folder.glob("*.summary.json"))
    summary_path = next((p for p in summaries if p.is_file()), None)
    if summary_path is None:
        sys.exit(f"{folder} hasn't been analysed (no .summary.json): run latency-analyzer {folder} first")
    stem = summary_path.name[: -len(".summary.json")]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    flips = read_rows(folder / f"{stem}.flips.csv")
    frames = []
    for row in read_rows(folder / f"{stem}.frames.csv"):
        try:
            frames.append(int(row["host_frame"]))
        except (KeyError, ValueError):
            pass
    # The folder's name, which may have been renamed to say what the run was, rather than the recording's
    run = Run(folder, label or folder.name, kind, summary, flips, sorted(set(frames)))
    run.stages = {s["label"]: s for s in (summary.get("pipeline") or {}).get("stages", [])}
    if kind == "auto":
        run.kind = "stream" if (run.sunshine or run.moonlight) else "unknown"
    return run


# ---------------------------------------------------------------------------------------------------------------------
# Where the time went in a run


def milestones(run):
    """
    The run's milestones as (name, ms after the host present), in order, each from the medians of its stages. Only
    those that can be placed on the host present's timeline: everything after the capture needs Sunshine's log (for
    the capture) and Moonlight's clock sync (for the client's times).
    """
    m = run.median
    capture = m(HOST_TO_CAPTURE)
    if capture is None:
        return []
    points = [("capture", capture)]
    client_present = m(CAPTURE_TO_CLIENT_PRESENT)
    if client_present is None:
        return points
    client_present += capture
    first = client_present - m(FIRST_TO_CLIENT_PRESENT) if m(FIRST_TO_CLIENT_PRESENT) is not None else None
    if first is None and m(CAPTURE_TO_FIRST) is not None:
        first = capture + m(CAPTURE_TO_FIRST)
    last = capture + m(CAPTURE_TO_LAST) if m(CAPTURE_TO_LAST) is not None else (
        first + m(FIRST_TO_LAST) if first is not None and m(FIRST_TO_LAST) is not None else None)
    handover = last + m(LAST_TO_HANDOVER) if last is not None and m(LAST_TO_HANDOVER) is not None else None
    gpu = handover + m(HANDOVER_TO_GPU) if handover is not None and m(HANDOVER_TO_GPU) is not None else None
    for name, value in (("first packet arrives", first), ("last packet arrives", last),
                        ("decoder hands it over", handover), ("GPU decode and draw done", gpu)):
        if value is not None:
            points.append((name, value))
    points.append(("client present", client_present))
    return points


SEGMENT_NAMES = {
    ("host present", "capture"): ("Host present → capture", HOST),
    ("capture", "first packet arrives"): ("Capture → first packet arrives", HOST),
    ("first packet arrives", "last packet arrives"): ("First → last packet", HOST),
    ("last packet arrives", "decoder hands it over"): ("Last packet → decoder hands it over", CLIENT),
    ("decoder hands it over", "GPU decode and draw done"): ("Decoder → GPU decode and draw done", CLIENT),
    ("GPU decode and draw done", "client present"): ("Pacer hold → client present", CLIENT),
    ("client present", "photon"): ("Client present → photon", DISPLAY),
}


def segments(run, direction):
    """
    The run's path for frames turning white or black, as (name, phase, ms) from the host present to the photon,
    adding up to the measured host present -> photon median. A run not broken down is one segment.
    """
    total = run.median(f"frames_to_{direction}")
    if total is None:
        return []
    if run.kind == "local":
        return [("Present → photon on the client", DISPLAY, total)]
    points = [("host present", 0.0)] + milestones(run)
    if len(points) == 1:
        return [("Host present → photon", WHOLE, total)]
    points.append(("photon", total))
    result = []
    for (a, ta), (b, tb) in zip(points, points[1:]):
        name, phase = SEGMENT_NAMES.get((a, b), (f"{a[0].upper()}{a[1:]} → {b}", WHOLE))
        if (a, b) == ("capture", "client present"):
            phase = HOST  # no client stages: Moonlight's capture to present, network and client together
            name = "Capture → client present (Moonlight)"
        elif b == "photon" and a == "capture":
            name, phase = "Capture → photon (network, client and display)", WHOLE
        result.append((name, phase, tb - ta))
    return result


def waterfall(run):
    """
    The run's path as (step, ms, ms after the host present) rows, when it is broken down: the steps both directions
    share, then the last step for each direction. [] for a run that isn't broken down.
    """
    black, white = segments(run, "black"), segments(run, "white")
    paths = [(d, s) for d, s in (("black", black), ("white", white)) if len(s) > 1]
    if not paths:
        return []
    shared = paths[0][1][:-1]
    rows, total = [], 0.0
    for name, _, value in shared:
        total += value
        rows.append((name, value, total))
    for direction, segs in paths:
        name, _, value = segs[-1]
        rows.append((f"{name}, to {direction}", value, total + value))
    return rows


# ---------------------------------------------------------------------------------------------------------------------
# Checks: does everything line up?


@dataclass
class Finding:
    level: str  # "ok", "info" or "warn"
    text: str


def changed_patch(frame, bits=8):
    """Which frame number patch changes for this frame: the number is Gray coded, patch 0 the top bit."""
    n = frame % (1 << bits)
    if n == 0:
        return 0
    return bits - 1 - ((n & -n).bit_length() - 1)


def not_seen_frames(run):
    """Host frames between the first and last seen that the camera never timed."""
    if len(run.frames) < 2:
        return []
    seen = set(run.frames)
    return [f for f in range(run.frames[0], run.frames[-1] + 1) if f not in seen]


def edge_times(run):
    """The trigger's mean 10-90% rise and fall times in ms, from the flips, or None."""
    out = {}
    for edge in ("rise", "fall"):
        values = []
        for row in run.flips:
            try:
                if row.get("edge") == edge:
                    values.append(float(row["edge_10_90_ms"]))
            except (KeyError, ValueError):
                pass
        out[edge] = float(np.mean(values)) if values else None
    return out if None not in out.values() else None


def display_delay(run):
    """
    The client display's own delay for each direction, where a run measures it: a local run's host present -> photon,
    or a stream's client present -> photon from Moonlight. A dict of direction -> (ms, what it is), or {}.
    """
    out = {}
    for direction in ("white", "black"):
        if run.kind == "local" and run.median(f"frames_to_{direction}") is not None:
            out[direction] = (run.median(f"frames_to_{direction}"), "present → photon, local")
        elif run.median(f"client present -> photon, to {direction}") is not None:
            out[direction] = (run.median(f"client present -> photon, to {direction}"), "client present → photon")
    return out


def run_checks(run):
    """Checks within one run."""
    found = []
    m = run.median
    # Moonlight's clock sync against Sunshine's own times: the last packet can't arrive before Sunshine sent it
    last = m(CAPTURE_TO_LAST)
    if last is None and m(CAPTURE_TO_FIRST) is not None and m(FIRST_TO_LAST) is not None:
        last = m(CAPTURE_TO_FIRST) + m(FIRST_TO_LAST)
    sent, encoded, first = m(CAPTURE_TO_SENT), m(CAPTURE_TO_ENCODED), m(CAPTURE_TO_FIRST)
    if last is not None and sent is not None:
        network = last - sent
        if network < -0.3:
            found.append(Finding("warn", f"{run.label}: Moonlight has the last packet arriving {-network:.2f} ms before "
                                         "Sunshine finished sending it, which can't be: Moonlight's clock sync is off by at "
                                         "least that much, and every stage built on it (capture → client present, client "
                                         "present → photon) with it."))
        elif network > 3:
            found.append(Finding("warn", f"{run.label}: the last packet arrived {network:.2f} ms after Sunshine sent it: "
                                         "a slow network, or Moonlight's clock sync off by that much."))
        else:
            found.append(Finding("ok", f"{run.label}: Moonlight's clock sync agrees with Sunshine: the last packet "
                                       f"arrived {network:.2f} ms after Sunshine finished sending it."))
        if first is not None and encoded is not None and first < encoded - 0.5:
            found.append(Finding("info", f"{run.label}: the first packet arrived {first:.2f} ms after capture, "
                                         f"before the encode finished ({encoded:.2f} ms): Sunshine sends the frame in "
                                         "parts while it is still being encoded."))
    # The client's decode and draw against the frame time
    gpu, interval = m(HANDOVER_TO_GPU), run.frame_interval()
    if gpu is not None and interval:
        if gpu > 0.8 * interval:
            found.append(Finding("warn", f"{run.label}: the client's GPU takes {gpu:.2f} ms to decode and draw each "
                                         f"frame, {gpu / interval:.0%} of the {interval:.2f} ms between frames: little "
                                         "headroom, so heavier frames or a busier GPU would fall behind."))
    dxgi = run.stages.get(CLIENT_PRESENT_TO_DISPLAY)
    if dxgi:
        level = "ok" if dxgi["median"] < 1.5 else "warn"
        found.append(Finding(level, f"{run.label}: DXGI has the client's presents reaching the display "
                                    f"{dxgi['median']:.2f} ms after the present (median)"
                                    + (", so nothing queues on the client." if level == "ok"
                                       else ": frames are queuing on the client.")))
    # The trigger and the frame patches disagree on which way the display is slower
    flip_white, flip_black = m("flips_to_white"), m("flips_to_black")
    frame_edge = run.summary.get("edge_difference_ms")  # going to black minus going to white
    if None not in (flip_white, flip_black, frame_edge):
        flip_edge = flip_black - flip_white
        if abs(flip_edge) > 1 and abs(frame_edge) > 1 and (flip_edge > 0) != (frame_edge > 0):
            times = edge_times(run)
            detail = (f" The trigger takes {times['rise']:.1f} ms from 10% to 90% going white and {times['fall']:.1f} ms "
                      "going black." if times else "")
            found.append(Finding("info", f"{run.label}: the frame patches have the display slower going to "
                                         f"{'black' if frame_edge > 0 else 'white'} ({abs(frame_edge):.1f} ms), the "
                                         f"trigger going to {'black' if flip_edge > 0 else 'white'} "
                                         f"({abs(flip_edge):.1f} ms).{detail} The trigger is a large square and the "
                                         "patches small, so this is most likely the display dimming large bright areas "
                                         "(local dimming or brightness limiting). The every frame figures, from the "
                                         "small patches, are the ones to go by."))
    # Frames not seen, and whether they share a patch (hard to read) or not (dropped)
    missing = not_seen_frames(run)
    if missing:
        listed = ", ".join(str(f) for f in missing[:12]) + (", ..." if len(missing) > 12 else "")
        # Half of all frames change the last patch, a quarter the one before it, and so on. Dropped frames land on
        # the patches in those proportions; misread ones on whichever patches are hard to read. When every missing
        # frame is a multiple of 2^k (so the last k patches never changed on them), random drops would do that with
        # probability 2^-k per frame.
        k = min(((f % 256) & -(f % 256)).bit_length() - 1 if f % 256 else 8 for f in missing)
        chance = 2.0 ** (-k * len(missing))
        patches = sorted({changed_patch(f) for f in missing})
        level = "warn" if len(missing) > 0.01 * max(len(run.frames), 1) else "info"
        if k >= 1 and chance < 0.01:
            found.append(Finding("info", f"{run.label}: {len(missing)} frames not seen ({listed}), all multiples of "
                                         f"{2 ** k}, so each was timed by one of patches {patches[0]}-{patches[-1]}, "
                                         f"never the last {k}. Frames dropped at random would do that with a chance of "
                                         f"{chance:.1g}, so these are most likely misread patches, not dropped frames."))
        else:
            found.append(Finding(level, f"{run.label}: {len(missing)} frames not seen ({listed}), spread over the "
                                        "patches as dropped frames would be: likely dropped or shown too briefly to time."))
    return found


def pair_checks(runs):
    """Checks across runs."""
    found = []
    delays = [(r, display_delay(r)) for r in runs]
    delays = [(r, d) for r, d in delays if d]
    for i, (a, da) in enumerate(delays):
        for b, db in delays[i + 1:]:
            directions = [d for d in ("black", "white") if d in da and d in db]
            if not directions:
                continue
            wa, wb = da[directions[0]][1], db[directions[0]][1]
            figures = "; ".join(f"going {d}, {a.label} {da[d][0]:.2f} ms and {b.label} {db[d][0]:.2f} ms "
                                f"({db[d][0] - da[d][0]:+.2f})" for d in directions)
            what = f"(the {wa} against the {wb})" if wa != wb else f"({wa})"
            if all(abs(da[d][0] - db[d][0]) <= 1.0 for d in directions):
                found.append(Finding("ok", f"The client display's own delay agrees {what}: {figures}."))
                continue
            text = f"The client display's own delay doesn't agree {what}: {figures}."
            local = a if a.kind == "local" else b if b.kind == "local" else None
            if local is not None and wa != wb:
                text += (f" {local.label}'s present time is when the test app called Present, before the client's GPU "
                         "had drawn the frame, so it includes the drawing, which Moonlight's present (made after "
                         "drawing) doesn't. If that's the cause, the gap shrinks when the local run is recorded with "
                         "--no-noise.")
            else:
                text += " The display's settings, the client's present mode or Moonlight's clock sync differ between the runs."
            found.append(Finding("warn", text))
    if any(r.kind == "stream" for r in runs):
        for r in runs:
            if r.kind == "local" and r.noise:
                found.append(Finding("info", f"{r.label} drew the background noise (amplitude {r.noise:g}). On the "
                                     "client's GPU that adds to its host present → photon, about 1 ms in one test, "
                                     "which the stream's client doesn't spend, so what streaming adds reads that much "
                                     "low. Record the local run with --no-noise: the noise only keeps a stream's "
                                     "encoder busy, and a local run has none."))
    return found


# ---------------------------------------------------------------------------------------------------------------------
# The comparison


HEADLINE = [
    ("Host present → photon, every frame, to black", "frames_to_black"),
    ("Host present → photon, every frame, to white", "frames_to_white"),
    ("Host present → photon, every frame", "frames"),
    ("Host present → photon, trigger flips", "flips"),
    ("Host present → photon, flips to white", "flips_to_white"),
    ("Host present → photon, flips to black", "flips_to_black"),
]


def headline_rows(runs):
    """(name, [value or None per run], unit, decimals) for the side by side table."""
    rows = []
    for name, key in HEADLINE:
        rows.append((name, [r.median(key) for r in runs], "ms", 2))
        rows.append(("  sd", [(r.summary.get(key) or {}).get("sd") for r in runs], "ms", 2))
    pacing = [r.summary.get("pacing") or {} for r in runs]
    rows.append(("Pacing, client − host interval, sd", [(p.get("mismatch") or {}).get("sd") for p in pacing], "ms", 2))
    rows.append(("Uneven intervals", [100 * p["uneven"] / p["pairs"] if p.get("pairs") else None for p in pacing], "%", 1))
    rows.append(("Frames not seen", [p.get("not_seen") for p in pacing], "", 0))
    rows.append(("Frames presented while filming", [p.get("frames_presented") for p in pacing], "", 0))
    rows.append(("Host frame interval", [r.frame_interval() for r in runs], "ms", 2))
    rows.append(("Inside Present", [((r.summary.get("present_wait") or {}).get("inside_present") or {}).get("median") for r in runs], "ms", 2))
    rows.append(("Display edge difference (to black − to white)", [r.summary.get("edge_difference_ms") for r in runs], "ms", 2))
    rows.append(("Clock sync, fastest round trip", [(r.summary.get("sync") or {}).get("min_rtt_ms") for r in runs], "ms", 2))
    return rows


def stage_rows(runs):
    """Every pipeline stage any run has: (label, [median or None per run]), in the analysis's order."""
    labels = []
    for r in runs:
        for label in r.stages:
            if label not in labels:
                labels.append(label)
    return [(label, [r.stages[label]["median"] if label in r.stages else None for r in runs]) for label in labels]


def streaming_pair(runs):
    """Whether the comparison is a local run against a stream, so the difference is what streaming adds."""
    return len(runs) == 2 and (runs[0].kind, runs[1].kind) == ("local", "stream")


def stream_part(run):
    """Host present -> client present: what the stream itself adds before the client display, or None."""
    points = dict(milestones(run))
    return points.get("client present")


def takeaways(runs):
    """Plain sentences: what the comparison says."""
    out = []
    a, b = runs[0], runs[-1]
    da = [(d, b.median(f"frames_to_{d}") - a.median(f"frames_to_{d}")) for d in ("black", "white")
          if a.median(f"frames_to_{d}") is not None and b.median(f"frames_to_{d}") is not None]
    if da:
        deltas = " and ".join(f"{v:+.1f} ms going {d}" for d, v in da)
        if (a.kind, b.kind) == ("local", "stream") and all(v >= 0 for _, v in da):
            added = " and ".join(f"{v:.1f} ms going {d}" for d, v in da)
            out.append(f"Streaming adds {added} to host present → photon (every frame, median) over running on the "
                       "client.")
        elif (a.kind, b.kind) == ("local", "stream"):
            out.append(f"Streaming changes host present → photon by {deltas} (every frame, median) against running "
                       "on the client.")
        else:
            out.append(f"{b.label} against {a.label}: host present → photon {deltas} (every frame, median).")
    for r in runs:
        part = stream_part(r)
        if part is not None and r.kind == "stream":
            out.append(f"In {r.label}, the stream takes {part:.1f} ms from the host present to the client present; "
                       "the rest is the client display.")
    local = [r for r in runs if r.kind == "local"]
    streams = [r for r in runs if r.kind == "stream" and stream_part(r) is not None]
    if local and streams and da:
        naive = dict(da).get("black")
        part = stream_part(streams[0])
        if naive is not None and abs(part - naive) > 1:
            out.append(f"That's {part - naive:+.1f} ms off the plain difference ({naive:.1f} ms going black) because the "
                       "two runs' present → photon on the client don't agree (see the checks): "
                       f"{part:.1f} ms is the stream's own part.")
    sds = [((r.summary.get("pacing") or {}).get("mismatch") or {}).get("sd") for r in runs]
    if None not in sds:
        if max(sds) - min(sds) < 0.1:
            out.append(f"Pacing is the same in every run (client − host interval sd {', '.join(f'{s:.2f}' for s in sds)} ms): "
                       f"{'the stream adds no measurable unevenness' if any(r.kind == 'local' for r in runs) else 'no run paces worse than another'}.")
        else:
            worst = runs[int(np.argmax(sds))]
            out.append(f"{worst.label} paces less evenly: client − host interval sd {max(sds):.2f} ms against {min(sds):.2f}.")
    with_stages = [r for r in runs if r.stages]
    if len(with_stages) >= 2:
        x, y = with_stages[0], with_stages[-1]
        changes = [(label, y.stages[label]["median"] - x.stages[label]["median"]) for label in x.stages
                   if label in y.stages and "photon" not in label]
        changes = sorted((c for c in changes if abs(c[1]) >= 0.5), key=lambda c: -abs(c[1]))[:3]
        if changes:
            out.append(f"Biggest stage changes, {y.label} against {x.label}: "
                       + "; ".join(f"{label.replace(' -> ', ' → ')} {delta:+.2f} ms" for label, delta in changes) + ".")
    for r in runs:
        segs = [s for s in segments(r, "black") if s[1] != WHOLE]
        if len(segs) > 2:
            top = sorted(segs, key=lambda s: -s[2])[:3]
            out.append(f"Largest parts of {r.label} (going black): " + "; ".join(f"{n} {v:.1f} ms" for n, _, v in top) + ".")
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Output


def fmt(value, decimals=2, unit=""):
    if value is None:
        return "-"
    return f"{value:.{decimals}f}" + (f" {unit}" if unit and unit != "%" else unit)


def text_report(runs, findings, notes):
    lines = ["Runs:"]
    for r in runs:
        lines.append(f"  {r.label}: {r.describe()}  ({r.folder})")
    lines.append("")
    width = max(len(n) for n, *_ in headline_rows(runs)) + 2
    delta_head = "        added" if streaming_pair(runs) else "            Δ"
    lines.append(" " * width + "".join(f"{r.label[:14]:>16}" for r in runs) + (delta_head if len(runs) == 2 else ""))
    for name, values, unit, decimals in headline_rows(runs):
        row = f"{name:<{width}}" + "".join(f"{fmt(v, decimals):>16}" for v in values)
        if len(runs) == 2 and None not in values and not name.startswith("  "):
            row += f"{values[1] - values[0]:>+13.{decimals}f}"
        lines.append(row)
    for r in runs:
        steps = waterfall(r)
        if steps:
            lines.append("")
            lines.append(f"{r.label}, where the time goes (medians, ms):")
            lines.extend(f"  {name:<50}{value:>7.2f}  ->{total:>7.2f}" for name, value, total in steps)
    lines.append("")
    lines.append("Checks:")
    for f in findings:
        lines.append(f"  [{f.level}] {f.text}")
    lines.append("")
    lines.append("Takeaways:")
    lines.extend(f"  - {n}" for n in notes)
    # Consoles on Windows are often cp1252
    return "\n".join(lines).replace("→", "->").replace("−", "-").replace("Δ", "diff")


COMPARE_STYLE = """
svg .seg.host { fill: var(--series); }
svg .seg.client { fill: var(--series-2); }
svg .seg.display { fill: var(--series-3); }
svg .seg.whole { fill: var(--muted); }
svg .bar-label { fill: var(--text); font-size: 12px; }
svg .seg-label { fill: #fff; font-size: 11px; }
:root { --series-3: #1baf7a; }
:root[data-theme="dark"] { --series-3: #199e70; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --series-3: #199e70; } }
.check { padding: 6px 12px; margin: 6px 0; border-radius: 4px; border-left: 3px solid var(--border); background: var(--surface); }
.check.warn { background: var(--warn-bg); border-left-color: var(--warn-border); }
.check .tag { font-weight: 600; margin-right: 6px; }
td.delta { color: var(--text-2); }
ul.takeaways li { margin: 4px 0; }
"""


def bars(runs):
    """An SVG of one stacked bar per run and direction, from the host present to the photon, on one scale."""
    rows = []
    for r in runs:
        for direction in ("black", "white"):
            segs = segments(r, direction)
            if segs:
                rows.append((f"{r.label}, to {direction}", segs))
    if not rows:
        return ""
    width, left, right, row_h, top, bottom = 760, 170, 20, 30, 8, 34
    height = top + bottom + row_h * len(rows)
    highest = max(sum(s[2] for s in segs) for _, segs in rows)
    step = 5 if highest <= 60 else 10
    top_value = step * (int(highest / step) + 1)
    x_of = lambda v: left + v / top_value * (width - left - right)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Host present to photon of each run, by stage">']
    v = 0
    while v <= top_value + 1e-9:
        x = x_of(v)
        parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{height - bottom}"/>')
        parts.append(f'<text class="tick" x="{x:.1f}" y="{height - bottom + 16}" text-anchor="middle">{v:g}</text>')
        v += step
    parts.append(f'<text class="tick" x="{width - right}" y="{height - 4}" text-anchor="end">ms after the host present</text>')
    for i, (label, segs) in enumerate(rows):
        y = top + i * row_h + 5
        h = row_h - 10
        parts.append(f'<text class="bar-label" x="{left - 8}" y="{y + h / 2 + 4:.1f}" text-anchor="end">{html.escape(label)}</text>')
        start = 0.0
        for name, phase, value in segs:
            x0, x1 = x_of(start), x_of(start + max(value, 0))
            tip = f"{name}: {value:.2f} ms (from {start:.2f} to {start + value:.2f})"
            # A 2 px surface gap between segments
            w = max(x1 - x0 - 2, 0.5)
            parts.append(f'<g class="hit" data-tip="{html.escape(tip)}"><rect class="seg {phase}" x="{x0:.1f}" y="{y:.1f}" '
                         f'width="{w:.1f}" height="{h}" rx="2"/>')
            if w > 34:
                parts.append(f'<text class="seg-label" x="{x0 + w / 2:.1f}" y="{y + h / 2 + 4:.1f}" text-anchor="middle">{value:.1f}</text>')
            parts.append("</g>")
            start += max(value, 0)
        parts.append(f'<text class="tick" x="{x_of(start) + 4:.1f}" y="{y + h / 2 + 4:.1f}">{start:.1f}</text>')
    parts.append("</svg>")
    used = []
    for _, segs in rows:
        for _, phase, _ in segs:
            if phase not in used:
                used.append(phase)
    legend = " &nbsp; ".join(f'<svg width="12" height="12"><rect width="12" height="12" rx="2" class="seg {p}" '
                             f'style="fill: var(--{ {HOST: "series", CLIENT: "series-2", DISPLAY: "series-3", WHOLE: "muted"}[p] })"/></svg> '
                             f'{html.escape(PHASE_NAMES[p])}' for p in (HOST, CLIENT, DISPLAY, WHOLE) if p in used)
    return "".join(parts) + f'<p class="legend">{legend}. Hover a segment for its stage. Medians of each stage, so a ' \
                            'bar adds up to its run&#39;s measured host present &rarr; photon median.</p>'


def html_report(path, runs, findings, notes):
    from report import SCRIPT, STYLE

    esc = html.escape
    arrow = lambda s: esc(s).replace("-&gt;", "&rarr;")
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>Compare {esc(" vs ".join(r.label for r in runs))}</title><style>{STYLE}{COMPARE_STYLE}</style></head><body><main>']
    out.append(f'<h1>{esc(" vs ".join(r.label for r in runs))}</h1>')
    out.append('<p class="sub">' + " &middot; ".join(f'{esc(r.label)}: {esc(r.describe())}' for r in runs) + "</p>")

    a, b = runs[0], runs[-1]
    tiles = []
    for d in ("black", "white"):
        va, vb = a.median(f"frames_to_{d}"), b.median(f"frames_to_{d}")
        if va is not None and vb is not None:
            if streaming_pair(runs):
                label = f"Added by streaming, host present &rarr; photon, to {d} ({esc(b.label)} over {esc(a.label)})"
            else:
                label = f"Host present &rarr; photon, to {d}, {esc(b.label)} &minus; {esc(a.label)}"
            tiles.append((label, f"{vb - va:+.2f} <small>ms</small>"))
    for r in runs:
        part = stream_part(r)
        if part is not None:
            tiles.append((f"{esc(r.label)}: host present &rarr; client present", f"{part:.2f} <small>ms</small>"))
    sds = [((r.summary.get("pacing") or {}).get("mismatch") or {}).get("sd") for r in runs]
    if None not in sds:
        tiles.append(("Pacing sd, " + " / ".join(esc(r.label) for r in runs), " / ".join(f"{s:.2f}" for s in sds) + " <small>ms</small>"))
    out.append('<div class="tiles">' + "".join(f'<div class="tile"><div class="label">{l}</div><div class="value">{v}</div></div>'
                                               for l, v in tiles) + "</div>")

    out.append("<h2>Takeaways</h2>")
    out.append('<div class="card"><ul class="takeaways">' + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul></div>")

    chart = bars(runs)
    if chart:
        out.append("<h2>Host present &rarr; photon, by stage</h2>")
        out.append(f'<div class="card">{chart}</div>')

    out.append("<h2>Side by side, medians</h2>")
    delta_head = "Added by streaming" if streaming_pair(runs) else "&Delta;"
    head = "".join(f"<th>{esc(r.label)}</th>" for r in runs) + (f"<th>{delta_head}</th>" if len(runs) == 2 else "")
    out.append(f'<div class="card"><table><thead><tr><th></th>{head}</tr></thead><tbody>')
    for name, values, unit, decimals in headline_rows(runs):
        if name.startswith("  "):
            continue
        cells = "".join(f"<td>{fmt(v, decimals, unit)}</td>" for v in values)
        if len(runs) == 2:
            cells += f'<td class="delta">{values[1] - values[0]:+.{decimals}f}</td>' if None not in values else "<td></td>"
        out.append(f"<tr><th>{esc(name)}</th>{cells}</tr>")
    out.append("</tbody></table></div>")

    for r in runs:
        steps = waterfall(r)
        if not steps:
            continue
        out.append(f"<h2>{esc(r.label)}: where the time goes</h2>")
        out.append('<div class="card"><table><thead><tr><th>Step</th><th>ms</th><th>after host present</th></tr></thead><tbody>')
        out.extend(f"<tr><th>{esc(name)}</th><td>{value:.2f}</td><td>{total:.2f}</td></tr>" for name, value, total in steps)
        out.append('</tbody></table><p class="legend">Medians of each stage. Only the last step depends on which way the '
                   'display changed.</p></div>')

    stages = stage_rows(runs)
    if stages:
        out.append("<h2>Pipeline stages, medians</h2>")
        out.append(f'<div class="card"><table><thead><tr><th>Stage</th>{head}</tr></thead><tbody>')
        for label, values in stages:
            cells = "".join(f"<td>{fmt(v)}</td>" for v in values)
            if len(runs) == 2:
                cells += f'<td class="delta">{values[1] - values[0]:+.2f}</td>' if None not in values else "<td></td>"
            out.append(f"<tr><th>{arrow(label)}</th>{cells}</tr>")
        out.append("</tbody></table></div>")

    out.append("<h2>Checks</h2>")
    tags = {"ok": "OK", "info": "Note", "warn": "Check"}
    out.extend(f'<div class="check {f.level}"><span class="tag">{tags[f.level]}</span>{esc(f.text)}</div>' for f in findings)
    if not findings:
        out.append('<p class="legend">Nothing to check: the runs have no stage logs and nothing stands out.</p>')

    out.append('<p class="files">Runs: ' + ", ".join(esc(r.folder.name) for r in runs) + "</p>")
    out.append(f'<div id="tip"></div></main><script>{SCRIPT}</script></body></html>')
    Path(path).write_text("\n".join(out), encoding="utf-8")


def compare(folders, labels=None, kinds=None, out=None):
    """Load the runs, check them and write the report; returns (text, report path)."""
    labels = labels or [None] * len(folders)
    kinds = kinds or ["auto"] * len(folders)
    return compare_runs([load_run(f, l, k) for f, l, k in zip(folders, labels, kinds)], out)


def ask_and_compare(folders, ask=input):
    """
    Compare the folders, asking what each run was when its logs don't say and what to call it, then offer to open the
    report. For running without arguments; ask is input().
    """
    runs = []
    for folder in folders:
        run = load_run(folder)
        if run.kind == "stream":
            print(f"\n{run.label}: a stream (it has {' and '.join(n for n, h in (('Sunshine', run.sunshine), ('Moonlight', run.moonlight)) if h)} logs)")
        else:
            while True:
                answer = ask(f"\nWhat was {run.label}? l = local (the test app on the client, no stream), s = a stream, "
                             "Enter = don't know: ").strip().lower()
                if answer in ("", "l", "s", "local", "stream"):
                    break
                print("Enter l, s or nothing.")
            run.kind = {"l": "local", "local": "local", "s": "stream", "stream": "stream"}.get(answer, "unknown")
        label = ask(f"Name for {run.label} in the report (Enter keeps the folder's name): ").strip()
        if label:
            run.label = label
        runs.append(run)
    text, path = compare_runs(runs)
    print()
    print(text)
    print(f"\nWrote {path}")
    if hasattr(os, "startfile") and ask("\nOpen the report? [Y/n] ").strip().lower() in ("", "y", "yes"):
        os.startfile(path)
    return path


def compare_runs(runs, out=None):
    """
    Check loaded runs and write the report; returns (text, report path). Local runs go first, so the baseline is a run
    without a stream when there is one, and the differences show what streaming adds.
    """
    runs = sorted(runs, key=lambda r: r.kind != "local")
    findings = [f for r in runs for f in run_checks(r)] + pair_checks(runs)
    findings.sort(key=lambda f: {"warn": 0, "info": 1, "ok": 2}[f.level])
    notes = takeaways(runs)
    text = text_report(runs, findings, notes)
    if out is None:
        out = runs[-1].folder.parent / ("compare_" + "_vs_".join(r.folder.name for r in runs) + ".html")
    html_report(out, runs, findings, notes)
    return text, Path(out)


def main(argv):
    parser = argparse.ArgumentParser(prog="latency-analyzer compare", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", type=Path, nargs="+",
                        help="analysed results folders, the first taken as the baseline (a local run always is)")
    parser.add_argument("--kinds", nargs="+", choices=("local", "stream", "auto"),
                        help="what each run was, in order: local (on the client, no stream), stream, or auto (stream "
                             "when it has Sunshine's or Moonlight's logs, else unknown)")
    parser.add_argument("--labels", nargs="+", help="a name for each run, in order (default: its folder's name)")
    parser.add_argument("--out", type=Path, help="the HTML report (default: compare_A_vs_B.html next to the last run's folder)")
    args = parser.parse_args(argv)
    if len(args.runs) < 2:
        parser.error("give at least two results folders")
    for name, values in (("--kinds", args.kinds), ("--labels", args.labels)):
        if values is not None and len(values) != len(args.runs):
            parser.error(f"{name} needs one value per run ({len(args.runs)})")
    text, path = compare(args.runs, args.labels, args.kinds, args.out)
    print(text)
    print(f"\nWrote {path}")
