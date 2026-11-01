"""
Timing for videos recorded with Latency Cam: when each row of each camera frame was exposed, on the host's
QueryPerformanceCounter clock, and the host's own logs on the same clock.

Latency Cam writes VIDEO.camera.json next to each video (see android/README.md). latency-test.exe writes an input log
and a .frames.csv with every frame's present time. Times here are in seconds; host times are seconds of QPC since the
host log's start_qpc, the same zero as the logs' microseconds.
"""
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------------------------------------------------
# The camera


@dataclass
class CameraTiming:
    """Exposure timing of every video frame, on the phone's elapsedRealtime clock."""

    starts_ns: np.ndarray  # start of exposure of the active array's first row, per video frame
    exposure_ns: np.ndarray
    skew_ns: np.ndarray  # first row to last row of the active array
    row_offset: np.ndarray  # fraction of the active array above the video's first row, per frame
    row_span: np.ndarray  # fraction of the active array the video's rows cover, per frame
    height: int  # video rows
    fps: float
    unmatched: int  # video frames without a capture result, timed from their neighbours
    info: dict = field(default_factory=dict)

    def row_time_ns(self, frame, row):
        """
        When a video row was exposed, at the middle of its exposure, for a fractional frame index. Rows are read out
        top to bottom of the video as stored, which is the sensor's own orientation.
        """
        k = int(np.clip(np.floor(frame), 0, len(self.starts_ns) - 1))
        k1 = min(k + 1, len(self.starts_ns) - 1)
        fraction = frame - k

        def at(i):
            position = self.row_offset[i] + self.row_span[i] * row / self.height
            return self.starts_ns[i] + self.skew_ns[i] * position + self.exposure_ns[i] / 2

        return at(k) + (at(k1) - at(k)) * fraction


def load_camera_timing(path):
    """Read a .camera.json file and line its capture results up with the video's frames."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "ml-latency-camera":
        raise ValueError(f"{path} is not a Latency Cam timing file")
    if data.get("timestamp_source") != "REALTIME":
        raise ValueError("the camera's timestamps are not on the REALTIME clock, so they can't be synced with the host")

    # The encoder's presentation times are on CLOCK_MONOTONIC; the capture results on elapsedRealtime
    clocks = [c for c in (data.get("clocks_at_start"), data.get("clocks_at_stop")) if c]
    if not clocks:
        raise ValueError("the timing file has no clock pairs; record it again with a newer Latency Cam")
    monotonic_to_realtime = float(np.median([c[0] - c[1] for c in clocks]))
    video_ns = np.sort(np.array(data["video_pts_us"], dtype=np.float64)) * 1000 + monotonic_to_realtime

    fields = data["capture_fields"]
    captures = np.array(data["captures"], dtype=np.float64)
    if captures.size == 0:
        raise ValueError("the timing file has no capture results")
    captures = captures[np.argsort(captures[:, 0])]
    column = {name: i for i, name in enumerate(fields)}

    # Each video frame's capture result is the one with the nearest timestamp, normally within tens of microseconds
    index = np.clip(np.searchsorted(captures[:, 0], video_ns), 1, len(captures) - 1)
    nearest = np.where(np.abs(captures[index, 0] - video_ns) < np.abs(captures[index - 1, 0] - video_ns), index, index - 1)
    matched = np.abs(captures[nearest, 0] - video_ns) < 1_000_000
    rows = captures[nearest]

    starts = np.where(matched, rows[:, 0], video_ns)
    exposure = np.where(matched, rows[:, column["exposure_ns"]], np.median(captures[:, column["exposure_ns"]]))
    skew = np.where(matched, rows[:, column["rolling_shutter_skew_ns"]], np.median(captures[:, column["rolling_shutter_skew_ns"]]))

    # Which rows of the sensor the video covers: a stream of another aspect ratio than the crop region shows the
    # largest centred part of the crop with the stream's aspect ratio. Without the crop region (older files), assume
    # the video covers all the rows.
    width, height = int(data["width"]), int(data["height"])
    active = data.get("active_array")
    if active and "crop_top" in column:
        crop_top = rows[:, column["crop_top"]]
        crop_width = rows[:, column["crop_width"]]
        crop_height = rows[:, column["crop_height"]]
        known = matched & (crop_height > 0) & (crop_width > 0)
        shown_height = np.minimum(crop_height, crop_width * height / width)
        shown_top = crop_top + (crop_height - shown_height) / 2
        row_offset = np.where(known, (shown_top - active[1]) / active[3], 0.0)
        row_span = np.where(known, shown_height / active[3], 1.0)
    else:
        row_offset = np.zeros(len(video_ns))
        row_span = np.ones(len(video_ns))

    return CameraTiming(
        starts_ns=starts,
        exposure_ns=exposure,
        skew_ns=skew,
        row_offset=row_offset,
        row_span=row_span,
        height=height,
        fps=float(data.get("fps", 0)) or 1e9 / float(np.median(np.diff(video_ns))),
        unmatched=int((~matched).sum()),
        info=data,
    )


# ---------------------------------------------------------------------------------------------------------------------
# The clock sync


@dataclass
class ClockFit:
    """host QPC seconds = phone ns * slope + intercept, fitted on the fastest exchanges."""

    slope: float
    intercept: float
    reference_ns: float  # phone time the fit is centred on, to keep the numbers small
    residual_ms: float  # spread of the chosen exchanges around the line
    min_rtt_ms: float
    median_rtt_ms: float
    windows: int  # exchanges used, one per window
    samples: int
    drift_ppm: float  # how much faster the phone's clock runs than the host's

    def host_seconds(self, phone_ns):
        return (np.asarray(phone_ns, dtype=np.float64) - self.reference_ns) * self.slope + self.intercept


def fit_clock(samples, frequency, window_s=1.0):
    """
    Fit the phone to host clock mapping from the raw exchanges (t1, t2, t3, t4): phone ns, host QPC ticks, host QPC
    ticks, phone ns. Each exchange gives the offset assuming the request and reply took equally long; queuing makes
    one leg longer, so only the fastest round trip of each window is used, and outliers are dropped before fitting
    a line, which takes out the drift between the two clocks.
    """
    s = np.asarray(samples, dtype=np.float64)
    if s.ndim != 2 or len(s) < 3:
        raise ValueError("too few clock sync samples; was the phone synced with the host while recording?")
    t1, t2, t3, t4 = s[:, 0], s[:, 1] * 1e9 / frequency, s[:, 2] * 1e9 / frequency, s[:, 3]
    rtt = (t4 - t1) - (t3 - t2)
    offset = ((t2 - t1) + (t3 - t4)) / 2  # host ns - phone ns
    middle = (t1 + t4) / 2

    window = np.floor((middle - middle[0]) / (window_s * 1e9)).astype(np.int64)
    chosen = []
    for w in np.unique(window):
        members = np.flatnonzero(window == w)
        chosen.append(members[np.argmin(rtt[members])])
    chosen = np.array(chosen)

    reference = float(np.median(middle))
    x = middle[chosen] - reference
    y = offset[chosen]
    keep = np.ones(len(chosen), dtype=bool)
    for _ in range(3):
        if keep.sum() >= 2:
            slope, intercept = np.polyfit(x[keep], y[keep], 1)
        else:
            slope, intercept = 0.0, float(np.median(y))
        residual = y - (slope * x + intercept)
        spread = max(1.4826 * np.median(np.abs(residual[keep] - np.median(residual[keep]))), 20_000)  # at least 20 us
        keep = np.abs(residual) < 4 * spread
    residual = residual[keep]

    # host seconds = (phone + offset) / 1e9, offset = slope * (phone - reference) + intercept
    return ClockFit(
        slope=(1 + slope) / 1e9,
        intercept=(reference + intercept) / 1e9,
        reference_ns=reference,
        residual_ms=float(residual.std()) / 1e6 if residual.size else float("nan"),
        min_rtt_ms=float(rtt.min()) / 1e6,
        median_rtt_ms=float(np.median(rtt)) / 1e6,
        windows=int(keep.sum()),
        samples=len(s),
        drift_ppm=-float(slope) * 1e6,
    )


# ---------------------------------------------------------------------------------------------------------------------
# The host's logs


@dataclass
class HostLog:
    """latency-test.exe's two logs, with times in seconds since start_qpc."""

    header: dict
    flips: np.ndarray  # rows of seq, source code, present call s, frame
    flip_sources: list
    frames: np.ndarray  # rows of frame, render s, present call s, present return s, input seq
    start_qpc: int
    frequency: int

    def qpc_to_seconds(self, qpc_seconds_absolute):
        """Convert absolute QPC seconds (ticks / frequency) to seconds since start_qpc."""
        return qpc_seconds_absolute - self.start_qpc / self.frequency


def parse_header(lines):
    """The key=value pairs in the log's comment lines."""
    header = {}
    for line in lines:
        if line.startswith("#"):
            for key, value in re.findall(r"(\w+)=(\S+)", line):
                header[key] = value
    return header


def frames_path_for(log_path):
    log_path = Path(log_path)
    name = log_path.name[:-4] if log_path.name.endswith(".csv") else log_path.name
    return log_path.with_name(name + ".frames.csv")


def load_host_log(log_path):
    """Read the input log and its .frames.csv."""
    log_path = Path(log_path)
    if log_path.name.endswith(".frames.csv"):
        log_path = log_path.with_name(log_path.name[: -len(".frames.csv")] + ".csv")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    header = parse_header(lines)
    if "start_qpc" not in header:
        raise ValueError(f"{log_path} has no start_qpc; is it a latency-test log?")

    flips, sources = [], []
    for row in csv.DictReader(line for line in lines if not line.startswith("#")):
        flips.append([int(row["seq"]), 0, float(row["present_call_us"]) / 1e6, int(row["frame"])])
        sources.append(row["source"])

    frames_path = frames_path_for(log_path)
    frames = []
    if frames_path.is_file():
        frame_lines = [line for line in frames_path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        for row in csv.DictReader(frame_lines):
            frames.append([int(row["frame"]), float(row["render_us"]) / 1e6, float(row["present_call_us"]) / 1e6,
                           float(row["present_return_us"]) / 1e6, int(row["input_seq"])])
    return HostLog(
        header=header,
        flips=np.array(flips, dtype=np.float64).reshape(-1, 4),
        flip_sources=sources,
        frames=np.array(frames, dtype=np.float64).reshape(-1, 5),
        start_qpc=int(header["start_qpc"]),
        frequency=int(header["qpc_frequency"]),
    )


@dataclass
class PresentWait:
    """How long the test app spent inside Present, and what that says about the host."""
    wait_ms: np.ndarray  # per frame: Present's return minus its call
    interval_ms: float  # median time between presents
    refresh_hz: float  # the host display's refresh rate; 0 when unknown
    kind: str  # "none", "vsync", "queue" or "limiter"
    note: str  # an explanation for the report, or "" when there is nothing to say


def present_wait(frames, header):
    """
    Judge the time the test app spent inside Present. frames: host log frame rows (frame, render, present call,
    present return, input seq) from the filmed span. header: the host log's header.

    Present returns at once unless something holds it: v-sync, a full queue of frames waiting for the display, or a
    frame limiter such as RTSS, which waits inside the app's Present call.
    """
    frames = np.asarray(frames, dtype=np.float64).reshape(-1, 5)
    wait = (frames[:, 3] - frames[:, 2]) * 1000
    interval = float(np.median(np.diff(frames[:, 2]))) * 1000 if len(frames) > 2 else 0.0
    try:
        refresh = float(header.get("refresh_hz", 0))
    except ValueError:
        refresh = 0.0
    median = float(np.median(wait)) if wait.size else 0.0
    if wait.size == 0 or interval <= 0 or median < interval / 2:
        return PresentWait(wait, interval, refresh, "none", "")
    held = f"Present held the test app for {median:.1f} ms of every {interval:.1f} ms frame"
    if header.get("vsync") in ("1", "true"):
        return PresentWait(wait, interval, refresh, "vsync",
                           f"{held}: v-sync was on, so each present waited for the display. That wait is counted in "
                           "the host present times here, as it would be in a game with v-sync on.")
    if refresh > 0 and abs(interval - 1000 / refresh) < 1000 / refresh * 0.015:
        return PresentWait(wait, interval, refresh, "queue",
                           f"Frames were queuing on the host. {held} while presenting at the display's refresh rate "
                           f"({refresh:g} Hz): the test app made frames a little faster than the display took them, "
                           "so each one waited behind frames already queued, adding up to a frame or two to every "
                           "latency here. A game running uncapped at the refresh rate with v-sync off does the same. "
                           "Cap the frame rate a little below the refresh rate (F, or a limiter such as RTSS) to "
                           "measure without it.")
    limiter = "RTSS's frame rate limit" if header.get("fps_source") == "rtss" else "a frame limiter such as RTSS"
    return PresentWait(wait, interval, refresh, "limiter",
                       f"{held}, at {1000 / interval:.1f} fps" + (f", below the display's {refresh:g} Hz" if refresh else "")
                       + f": most likely {limiter}, which waits inside the app's Present. Host present times are "
                       "when the app called Present, so they leave the wait out when the limiter hands the frame on "
                       "before waiting, as RTSS's default mode appears to.")


# ---------------------------------------------------------------------------------------------------------------------
# Matching what the camera saw to what the host presented


def match_trigger_flips(camera_times, camera_rising, flips, max_latency=1.0):
    """
    For each trigger flip the camera saw (host seconds, and whether it turned white), the host flip it shows: the
    latest one presented before it whose sequence number gives the same colour (odd turns the trigger white), within
    max_latency. Returns the index into flips, or None, per camera flip, each host flip used at most once.
    """
    if len(flips) == 0:
        return [None] * len(camera_times)
    present = flips[:, 2]
    white = flips[:, 0].astype(np.int64) % 2 == 1
    used = set()
    result = []
    for t, rising in zip(camera_times, camera_rising):
        candidates = np.flatnonzero((present < t) & (present > t - max_latency) & (white == rising))
        pick = None
        for i in candidates[::-1]:
            if i not in used:
                pick = int(i)
                break
        if pick is not None:
            used.add(pick)
        result.append(pick)
    return result


def gray_encode(value):
    return value ^ (value >> 1)


def changed_bit(frame, bits=8):
    """Index, most significant first, of the frame number patch that changes when frame - 1 gives way to frame."""
    mask = (1 << bits) - 1
    diff = gray_encode(frame & mask) ^ gray_encode((frame - 1) & mask)
    return bits - diff.bit_length()


def assign_frames(appearances, frames, max_latency=1.0):
    """
    Give each appearance the full frame number of the host frame it shows. appearances: (host seconds, value mod
    256). frames: the host's frames log. The frame with that value presented last before the appearance is picked;
    the value repeats every 256 frames, two seconds or more, which is far longer than any latency.
    Returns the row index into frames, or None, per appearance.
    """
    if len(frames) == 0:
        return [None] * len(appearances)
    numbers = frames[:, 0].astype(np.int64)
    present = frames[:, 2]
    by_value = {}
    for i, n in enumerate(numbers):
        by_value.setdefault(int(n) & 255, []).append(i)
    by_value = {k: np.array(v) for k, v in by_value.items()}
    result = []
    for t, value in appearances:
        rows = by_value.get(int(value) & 255)
        if rows is None:
            result.append(None)
            continue
        before = rows[(present[rows] < t) & (present[rows] > t - max_latency)]
        result.append(int(before[-1]) if before.size else None)
    return result


# ---------------------------------------------------------------------------------------------------------------------
# Timing every host frame from its frame number patches


def normalize(values):
    """Scale a patch's brightness so its black level is 0 and its white level 1."""
    low, high = np.percentile(values, 2), np.percentile(values, 98)
    return (values - low) / max(high - low, 1e-6)


def refine_crossing(values, k, level):
    """
    Where values cross level between samples k and k + 1, from the cubic through samples k - 1 to k + 2. A display's
    response curves (fast at first, then settling), and a straight line between two samples 4 ms apart crosses
    halfway noticeably late; the cubic follows the curve. Falls back to the straight line at the ends or when the
    cubic doesn't cross inside the interval.
    """
    a, b = values[k], values[k + 1]
    linear = k + (level - a) / (b - a) if b != a else float(k)
    if k < 1 or k + 2 >= len(values):
        return linear
    window = values[k - 1:k + 3]
    if np.any(np.isnan(window)):
        return linear
    coefficients = np.polyfit([-1.0, 0.0, 1.0, 2.0], window - level, 3)
    roots = [r.real for r in np.roots(coefficients) if abs(r.imag) < 1e-9 and -1e-9 <= r.real <= 1 + 1e-9]
    if not roots:
        return linear
    return k + min(roots, key=lambda r: abs(r - (linear - k)))


def trigger_crossing(values, position):
    """Refine a trigger flip found by find_flips, at the midpoint between the levels either side of it."""
    k = int(np.clip(np.floor(position), 0, len(values) - 2))
    before = values[max(0, k - 12):max(1, k - 3)]
    after = values[k + 4:k + 13]
    if before.size == 0 or after.size == 0:
        return position
    level = (float(np.median(before)) + float(np.median(after))) / 2
    for j in (k, k - 1, k + 1):
        if 0 <= j < len(values) - 1 and min(values[j], values[j + 1]) <= level <= max(values[j], values[j + 1]):
            return refine_crossing(values, j, level)
    return position


def bit_crossings(values, low=0.3, high=0.7):
    """
    Each change of a frame number patch: the fractional camera frame where its normalized brightness crossed 0.5,
    and whether it turned white. A change only counts once it has gone past the far threshold, so noise around 0.5
    doesn't make extra ones.
    """
    crossings = []
    state = None
    last_side = None  # last frame firmly on the current side
    for i, v in enumerate(values):
        if np.isnan(v):
            continue
        if state is None:
            if v >= high or v <= low:
                state, last_side = v >= high, i
            continue
        if (state and v >= high) or (not state and v <= low):
            last_side = i
            continue
        if (state and v <= low) or (not state and v >= high):
            rising = not state
            # The crossing of 0.5 between the last firm frame on the old side and this one
            for k in range(last_side, i):
                a, b = values[k], values[k + 1]
                if not np.isnan(a) and not np.isnan(b) and (a < 0.5 <= b if rising else a > 0.5 >= b):
                    crossings.append((refine_crossing(values, k, 0.5), rising))
                    break
            state, last_side = rising, i
    return crossings


def decode_frame_changes(bit_values, bits=8):
    """
    Find each host frame's arrival on the client from the frame number patches. bit_values: normalized brightness
    per patch (most significant first), one array per patch. Only one patch changes from one frame to the next, so
    every change of a patch is one frame arriving: its value is read from the other patches just after the change,
    and changes that don't fit the Gray code (blurred or misread) are dropped.
    Returns (fractional camera frame, frame number mod 2^bits, patch index) for each arrival, in time order.
    """
    count = len(bit_values[0])
    arrivals = []
    for b, values in enumerate(bit_values):
        for position, rising in bit_crossings(values):
            k = min(int(np.ceil(position)), count - 1)
            code = 0
            for other, other_values in enumerate(bit_values):
                lit = rising if other == b else other_values[k] > 0.5
                code = (code << 1) | int(lit)
            value = gray_decode(code)
            if changed_bit(value, bits) == b:
                arrivals.append((position, value, b))
    arrivals.sort()
    return arrivals


def gray_decode(gray):
    value = gray
    shift = 1
    while shift < 32:
        value ^= value >> shift
        shift <<= 1
    return value


@dataclass
class Pacing:
    """How evenly the client showed the host's frames."""

    frames_seen: int
    frames_presented: int  # host frames presented between the first and last one seen
    not_seen: int
    client_intervals_ms: np.ndarray  # between consecutive frames that were both seen
    host_intervals_ms: np.ndarray  # the same pairs, between their present calls
    mismatch_ms: np.ndarray  # client interval - host interval
    uneven: int  # pairs whose mismatch exceeds a quarter of the median host interval


def pacing_stats(frame_numbers, photon_s, present_s, presented_numbers):
    """
    Compare when consecutive host frames reached the client display with when the host presented them.
    frame_numbers, photon_s, present_s: per seen frame. presented_numbers: every frame number the host logged.
    """
    order = np.argsort(frame_numbers)
    numbers = np.asarray(frame_numbers)[order]
    photon = np.asarray(photon_s)[order]
    present = np.asarray(present_s)[order]
    consecutive = np.flatnonzero(np.diff(numbers) == 1)
    client = (photon[consecutive + 1] - photon[consecutive]) * 1000
    host = (present[consecutive + 1] - present[consecutive]) * 1000
    mismatch = client - host
    presented = np.asarray(presented_numbers)
    if numbers.size:
        in_range = int(np.sum((presented >= numbers[0]) & (presented <= numbers[-1])))
    else:
        in_range = 0
    threshold = np.median(host) / 4 if host.size else 0
    return Pacing(
        frames_seen=int(np.unique(numbers).size),
        frames_presented=in_range,
        not_seen=max(in_range - int(np.unique(numbers).size), 0),
        client_intervals_ms=client,
        host_intervals_ms=host,
        mismatch_ms=mismatch,
        uneven=int(np.sum(np.abs(mismatch) > threshold)) if host.size else 0,
    )


def patch_turned_white(frame, bit, bits=8):
    """Whether the frame number patch that changed for this frame went from black to white."""
    return bool((gray_encode(frame & ((1 << bits) - 1)) >> (bits - 1 - bit)) & 1)


def edge_offsets(patches, rising, latencies, min_count=8):
    """
    How much later than the overall median each kind of edge reaches halfway. Displays take longer to settle one way
    than the other (often 2-3 ms between white to black and black to white), and each frame is timed by whichever
    patch changed with it, so without this the frame-to-frame intervals swing by that difference even when frames
    arrive evenly. The offset is the median latency of each patch and direction, or of the direction alone when a
    patch has fewer than min_count of them.
    Returns a dict from (patch, rising) to its offset in ms, and the offsets of the two directions.
    """
    latencies = np.asarray(latencies, dtype=np.float64)
    patches = np.asarray(patches)
    rising = np.asarray(rising, dtype=bool)
    if latencies.size == 0:
        return {}, {True: 0.0, False: 0.0}
    overall = float(np.median(latencies))
    by_direction = {}
    for direction in (True, False):
        values = latencies[rising == direction]
        by_direction[direction] = float(np.median(values)) - overall if values.size else 0.0
    offsets = {}
    for patch in np.unique(patches):
        for direction in (True, False):
            values = latencies[(patches == patch) & (rising == direction)]
            offsets[(int(patch), direction)] = float(np.median(values)) - overall if values.size >= min_count else by_direction[direction]
    return offsets, by_direction
