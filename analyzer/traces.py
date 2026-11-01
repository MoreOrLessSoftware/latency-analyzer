"""
Sunshine's frame log and Moonlight's pacing trace, joined frame by frame with what the camera saw.

- latency-test.exe logs each frame's present call on QueryPerformanceCounter.
- Sunshine's sunshine-frames-*.csv logs each captured frame's present time on the same clock (present_us, QPC in
  microseconds), keyed by its stream timestamp rtp_us, with encode and send times in microseconds after capture.
- Moonlight's Moonlight-pacing-*.csv logs each frame by the same stream timestamp (host_us), with when its packets
  arrived, when it was decoded and presented, on the client's own clock.

So a test app frame is matched to the capture presented just after it, and that capture to Moonlight's row. The
client's clock has an unknown offset from the host's, so client times are only compared with each other, and with
the camera through their spread.
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# The names of the end to end stages. "host present" is the game (the test app) handing its frame to Windows on the
# host, "client present" Moonlight handing the decoded frame to Windows on the client: Moonlight's "capture to
# present" ends at the client present, while the camera's figures run to the light on the client's screen.
HOST_PRESENT_TO_CAPTURE = "host present -> capture"
CAPTURE_TO_CLIENT_PRESENT = "capture -> client present (Moonlight's capture to present)"
CLIENT_PRESENT_TO_PHOTON = "client present -> photon"
CAPTURE_TO_PHOTON = "capture -> photon"
HOST_PRESENT_TO_PHOTON = "host present -> photon"

# Moonlight stages: label, start column, end column. Missing columns (older builds) leave the stage out.
CLIENT_STAGES = [
    ("first packet -> last packet", "first_packet_us", "last_packet_us"),
    ("last packet -> decoder hands it over", "last_packet_us", "decoded_us"),
    ("handed over -> GPU decode finished", "decoded_us", "arrival_us"),
    ("ready -> client present (pacer hold)", "arrival_us", "present_us"),
    ("first packet -> client present", "first_packet_us", "present_us"),
]
# Sunshine stages, in microseconds after capture
HOST_STAGES = [
    ("capture -> encoded", "encoded_us"),
    ("capture -> sent", "sent_us"),
]


def read_rows(path):
    """A CSV's rows, skipping comment lines."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(line for line in f if line.strip() and not line.startswith("#")))


def number(row, key):
    """A column as a float, or None when it is missing, empty or negative (the logs' 'unknown')."""
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


@dataclass
class Stage:
    """One step of the pipeline, in ms per joined frame."""

    label: str
    source: str
    values: np.ndarray

    def stats(self):
        v = self.values
        return {"label": self.label, "source": self.source, "n": int(v.size), "median": float(np.median(v)),
                "sd": float(v.std()), "p10": float(np.percentile(v, 10)), "p90": float(np.percentile(v, 90))}


@dataclass
class Pipeline:
    """The joined traces: stages, and how well the camera and Moonlight agree frame by frame."""

    sunshine_file: str  # None when there was no Sunshine frame log
    moonlight_file: str  # None when there was no Moonlight pacing trace
    camera_frames: int
    joined: int
    stages: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def summary(self):
        return {
            "sunshine_frames": self.sunshine_file,
            "moonlight_trace": self.moonlight_file,
            "camera_frames": self.camera_frames,
            "joined": self.joined,
            "stages": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.stats().items()} for s in self.stages],
            "checks": {k: round(v, 3) for k, v in self.checks.items()},
        }


def match_captures(app_present_us, capture_present_us, before_us=2000, after_us=8000):
    """
    For each test app present (QPC us), the index of the capture presented with it: the first capture no more than
    before_us earlier and no more than after_us later, or -1. capture_present_us must be sorted.
    """
    capture = np.asarray(capture_present_us, dtype=np.float64)
    result = []
    for t in app_present_us:
        i = int(np.searchsorted(capture, t - before_us))
        result.append(i if i < len(capture) and capture[i] - t <= after_us else -1)
    return result


def align_moonlight(app_present_us, moonlight_rows, tolerance_us=1000.0):
    """
    Without Sunshine's log, find which Moonlight row shows which test app frame. Moonlight's host_us is the frame's
    stream timestamp: the host's clock, from an unknown zero. Captures follow the app's presents closely, so the host
    timestamps repeat the frame-to-frame jitter of the app's presents; the offset that lines the two patterns up
    best, with the most presents within tolerance_us of a timestamp and the smallest typical error, is taken.
    Returns (one row or None per present, the median error in ms, how much worse the runner-up offset was).
    """
    rows = sorted((r for r in moonlight_rows if number(r, "host_us") is not None), key=lambda r: float(r["host_us"]))
    if not rows or not app_present_us:
        return [None] * len(app_present_us), None, None
    host = np.array([float(r["host_us"]) for r in rows])
    present = np.asarray(app_present_us, dtype=np.float64)
    anchor = present[len(present) // 2]

    def fit(offset):
        target = present + offset
        right = np.clip(np.searchsorted(host, target), 1, len(host) - 1)
        left = right - 1
        pick = np.where(np.abs(host[left] - target) <= np.abs(host[right] - target), left, right)
        return pick, np.abs(host[pick] - target)

    scores = []
    for h in host:
        _, error = fit(h - anchor)
        hits = error <= tolerance_us
        if hits.sum() >= 3:
            scores.append((int(hits.sum()), float(np.median(error[hits])), h - anchor))
    if not scores:
        return [None] * len(present), None, None
    most = max(s[0] for s in scores)
    # Among the offsets matching nearly as many presents, the one with the smallest errors
    contenders = sorted((s for s in scores if s[0] >= most * 0.9), key=lambda s: s[1])
    best = contenders[0]
    runner_up = contenders[1][1] / max(best[1], 1e-6) if len(contenders) > 1 else None
    pick, error = fit(best[2])
    result = [None] * len(present)
    used = set()
    for i in np.argsort(error):
        if error[i] <= tolerance_us and pick[i] not in used:
            used.add(int(pick[i]))
            result[i] = rows[pick[i]]
    return result, best[1] / 1000, runner_up


def join(frame_rows, start_us, sunshine_rows=None, moonlight_rows=None):
    """
    Join the camera's frames with Sunshine's rows, Moonlight's, or both. frame_rows: dicts with present_s and
    photon_s (seconds since the host log's start), latency and rising. start_us: the host log's start_qpc in
    microseconds. With both logs, a frame needs a row in each. Returns a list of dicts holding the camera frame and
    its Sunshine row (or None) and Moonlight row (or None), and the alignment's quality when Moonlight's trace was
    lined up without Sunshine's log.
    """
    app_present = [f["present_s"] * 1e6 + start_us for f in frame_rows]
    joined = []
    alignment = None
    if sunshine_rows:
        captures = sorted((r for r in sunshine_rows if number(r, "present_us") is not None), key=lambda r: float(r["present_us"]))
        capture_present = [float(r["present_us"]) for r in captures]
        by_host = {}
        for r in moonlight_rows or []:
            key = number(r, "host_us")
            if key is not None:
                by_host[int(key)] = r
        for f, t, i in zip(frame_rows, app_present, match_captures(app_present, capture_present)):
            if i < 0:
                continue
            m = None
            if moonlight_rows:
                rtp = number(captures[i], "rtp_us")
                m = by_host.get(int(rtp)) if rtp is not None else None
                if m is None:
                    continue
            joined.append({"camera": f, "app_present_us": t, "photon_us": f["photon_s"] * 1e6 + start_us,
                           "capture": captures[i], "capture_us": capture_present[i], "moonlight": m})
    elif moonlight_rows:
        matched, error_ms, runner_up = align_moonlight(app_present, moonlight_rows)
        alignment = {"error_ms": error_ms, "runner_up": runner_up}
        for f, t, m in zip(frame_rows, app_present, matched):
            if m is not None:
                joined.append({"camera": f, "app_present_us": t, "photon_us": f["photon_s"] * 1e6 + start_us,
                               "capture": None, "capture_us": None, "moonlight": m})
    if joined and alignment:
        joined[0]["alignment"] = alignment
    return joined


def analyse(joined, camera_frames, sunshine_file, moonlight_file):
    """Work out the stages and cross-checks from joined frames. Either file name may be None."""
    pipeline = Pipeline(sunshine_file, moonlight_file, camera_frames, len(joined))
    if not joined:
        logs = " and ".join(x for x in ("Sunshine's frame log" if sunshine_file else None,
                                        "Moonlight's pacing trace" if moonlight_file else None) if x)
        pipeline.notes.append(f"No frame could be followed through {logs}: check that it is from the run that was filmed.")
        return pipeline

    def stage(label, source, values):
        values = [v for v in values if v is not None]
        if values:
            pipeline.stages.append(Stage(label, source, np.array(values, dtype=np.float64)))

    with_capture = [j for j in joined if j["capture"] is not None]
    with_moonlight = [j for j in joined if j["moonlight"] is not None]

    def capture_to_present(j):
        """Moonlight's capture to present for a frame, in ms, from its clock sync; None where it has none."""
        value = number(j["moonlight"], "capture_to_present_us") if j["moonlight"] is not None else None
        return None if value is None or value <= 0 else value / 1000

    def first_packet_to_present(j):
        a, b = number(j["moonlight"], "first_packet_us"), number(j["moonlight"], "present_us")
        return None if not a or not b else (b - a) / 1000

    if with_capture:
        stage(HOST_PRESENT_TO_CAPTURE, "test app + Sunshine",
              [(j["capture_us"] - j["app_present_us"]) / 1000 for j in with_capture])
        for label, column in HOST_STAGES:
            stage(label, "Sunshine", [None if number(j["capture"], column) is None else number(j["capture"], column) / 1000 for j in with_capture])
    # Where the frame left the host and crossed the network: Moonlight's capture to present less its own part
    stage("capture -> first packet", "Moonlight",
          [None if capture_to_present(j) is None or first_packet_to_present(j) is None
           else capture_to_present(j) - first_packet_to_present(j) for j in with_moonlight])
    for label, start, end in CLIENT_STAGES:
        values = []
        for j in with_moonlight:
            a, b = number(j["moonlight"], start), number(j["moonlight"], end)
            values.append(None if a is None or b is None or a == 0 or b == 0 else (b - a) / 1000)
        stage(label, "Moonlight", values)
    stage(CAPTURE_TO_CLIENT_PRESENT, "Moonlight", [capture_to_present(j) for j in with_moonlight])
    for direction, rising in (("to white", True), ("to black", False)):
        # The client display's own part: the camera's capture to photon less Moonlight's capture to present
        stage(f"{CLIENT_PRESENT_TO_PHOTON}, {direction}", "camera + Sunshine + Moonlight",
              [None if capture_to_present(j) is None else (j["photon_us"] - j["capture_us"]) / 1000 - capture_to_present(j)
               for j in with_capture if j["moonlight"] is not None and j["camera"]["rising"] == rising])
    for direction, rising in (("to white", True), ("to black", False)):
        stage(f"{CAPTURE_TO_PHOTON}, {direction}", "camera + Sunshine",
              [(j["photon_us"] - j["capture_us"]) / 1000 for j in with_capture if j["camera"]["rising"] == rising])
    for direction, rising in (("to white", True), ("to black", False)):
        stage(f"{HOST_PRESENT_TO_PHOTON}, {direction}", "camera",
              [j["camera"]["latency"] for j in joined if j["camera"]["rising"] == rising])

    if with_capture:
        app_to_capture = np.array([(j["capture_us"] - j["app_present_us"]) / 1000 for j in with_capture])
        if np.median(app_to_capture) < -1 or np.median(app_to_capture) > 20:
            pipeline.notes.append(f"Sunshine's captures came {np.median(app_to_capture):.1f} ms after the test app's "
                                  "presents, which doesn't fit: is Sunshine's present_us on QueryPerformanceCounter?")

    # The client's clock is offset from the host's by an unknown constant, so compare spreads: if the camera and
    # Moonlight see the same frame timing, photon - Moonlight's present is the same for every frame
    presents = [(j, number(j["moonlight"], "present_us")) for j in with_moonlight]
    presents = [(j, p) for j, p in presents if p]
    if presents:
        captured = [(j, p) for j, p in presents if j["capture"] is not None]
        if captured:
            through = np.array([(p - j["capture_us"]) / 1000 for j, p in captured])
            pipeline.checks["moonlight_capture_to_present_sd_ms"] = float(through.std())
        for direction, rising in (("white", True), ("black", False)):
            k = np.array([(j["photon_us"] - p) / 1000 for j, p in presents if j["camera"]["rising"] == rising])
            if k.size:
                pipeline.checks[f"photon_minus_present_sd_to_{direction}_ms"] = float(k.std())

    alignment = joined[0].get("alignment")
    if alignment and alignment["error_ms"] is not None:
        pipeline.checks["moonlight_alignment_error_ms"] = alignment["error_ms"]
        if alignment["runner_up"] is not None and alignment["runner_up"] < 1.5:
            pipeline.notes.append("Without Sunshine's frame log, Moonlight's trace was lined up with the test app's "
                                  "frames by the pattern of their timing, and the best fit was only slightly better than "
                                  f"the next ({alignment['runner_up']:.1f} times): its rows may be a frame off. Add "
                                  "Sunshine's sunshine-frames log to be sure.")
    return pipeline


def find(folders, sunshine=None, moonlight=None):
    """
    The Sunshine frame logs and Moonlight pacing traces to try: the given files, or every one in the folders.
    Returns two lists of paths.
    """
    def collect(given, pattern):
        if given:
            return [Path(given)]
        found = []
        for folder in folders:
            found += sorted(Path(folder).glob(pattern))
        return list(dict.fromkeys(found))

    return collect(sunshine, "sunshine-frames-*.csv"), collect(moonlight, "Moonlight-pacing-*.csv")


def build(frame_rows, start_us, sunshine_paths, moonlight_paths):
    """
    Join the camera's frames with whichever of Sunshine's frame log and Moonlight's pacing trace there are, trying
    every combination and keeping the one that follows the most frames; None when there are neither.
    """
    if not sunshine_paths and not moonlight_paths:
        return None
    sunshine = [(p, read_rows(p)) for p in sunshine_paths] or [(None, None)]
    moonlight = [(p, read_rows(p)) for p in moonlight_paths] or [(None, None)]
    best = None
    for s_path, s_rows in sunshine:
        for m_path, m_rows in moonlight:
            joined = join(frame_rows, start_us, s_rows, m_rows)
            if best is None or len(joined) > len(best[0]):
                best = (joined, s_path, m_path)
    joined, s_path, m_path = best
    return analyse(joined, len(frame_rows), s_path.name if s_path else None, m_path.name if m_path else None)


def sources(pipeline):
    """The logs the pipeline came from, in words."""
    return " and ".join(name for name in (pipeline.sunshine_file, pipeline.moonlight_file) if name)


def describe(pipeline):
    """Plain text lines for the console."""
    lines = [f"Pipeline:    {pipeline.joined} of {pipeline.camera_frames} frames followed through {sources(pipeline)}"]
    label_width = max((len(s.label) for s in pipeline.stages), default=0)
    source_width = max((len(s.source) for s in pipeline.stages), default=0)
    for s in pipeline.stages:
        st = s.stats()
        lines.append(f"  {st['label']:{label_width}s} {st['source']:{source_width}s} median {st['median']:6.2f}  sd {st['sd']:5.2f} ms")
    checks = pipeline.checks
    if any(key.endswith("sd_ms") or key.startswith("photon_minus") for key in checks):
        lines.append("  frame by frame (spread, so the client's clock offset doesn't matter):")
        if "moonlight_capture_to_present_sd_ms" in checks:
            lines.append(f"    client present - capture: sd {checks['moonlight_capture_to_present_sd_ms']:.2f} ms")
        for direction in ("white", "black"):
            key = f"photon_minus_present_sd_to_{direction}_ms"
            if key in checks:
                lines.append(f"    photon - client present, to {direction}: sd {checks[key]:.2f} ms")
    if "moonlight_alignment_error_ms" in checks:
        lines.append(f"  Moonlight's trace lined up with the test app's frames by timing, typical error {checks['moonlight_alignment_error_ms']:.2f} ms")
    return lines
