#!/usr/bin/env python3
"""
Measure streaming latency from a slow motion video of the latency-test pattern.

Film the client display while it streams Sunshine's latency-test app with Moonlight's latency marker turned on
(Ctrl+Alt+Shift+T), pressing keys now and then. Every press flips the marker square on the client right away and the
trigger square in the stream once the host's answer arrives; this script finds both flips in every input and reports
the time between them.

How it works:
 - The four corner bullseyes are found in every frame, so the camera may be handheld. They give a perspective
   transform from the stream's pixel grid to the camera image, and each frame is resampled onto that grid.
 - The trigger square's position comes from the same layout code as the app (pattern.h). The marker is placed by
   the client and depends on the client display, so it is found instead: the large square outside the pattern whose
   brightness flips the most over the video.
 - Each flip is timed where the brightness crosses halfway between the levels before and after it, interpolated
   between camera frames. Display response, camera exposure and scanout all smear a flip over several frames, but
   they smear both squares alike, so the halfway points stay comparable.

Requires numpy and opencv-python (or opencv-python-headless).

Usage:
  python analyze_video.py VIDEO --stream 2560x1440 [--fps 240]
"""
import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


# ---------------------------------------------------------------------------------------------------------------------
# Test pattern layout, ported from tools/latency_test/pattern.h. Keep the two in sync.


@dataclass
class Rect:
    """An axis aligned rectangle in stream pixels."""

    x: float
    y: float
    w: float
    h: float

    @property
    def center(self):
        return self.x + self.w / 2, self.y + self.h / 2

    def inset(self, fx, fy):
        """The middle part of the rectangle, keeping fraction fx of its width and fy of its height."""
        return Rect(self.x + self.w * (1 - fx) / 2, self.y + self.h * (1 - fy) / 2, self.w * fx, self.h * fy)


def align_down(value, grid):
    return value - value % grid


def compute_layout(width, height):
    """Where the latency-test app draws each element, for a stream of the given size."""
    raw_cell = min(width, height) // 8
    grid = 64 if raw_cell >= 64 else 16
    cell = max(align_down(raw_cell, max(grid, 32)), 32)
    half = cell // 2

    trigger = Rect(align_down(width // 2 - cell, grid), align_down(height // 2 - cell, grid), 2 * cell, 2 * cell)
    row_x = trigger.x - cell
    frame_row_y = trigger.y - cell
    input_row_y = trigger.y + trigger.h + half
    quiet = Rect(row_x - half, frame_row_y - half, 5 * cell, input_row_y + cell - (frame_row_y - half))

    right = align_down(width - grid - cell, grid)
    bottom = align_down(height - grid - cell, grid)
    readout_x = 2 * grid + cell
    return {
        "cell": cell,
        "trigger": trigger,
        "quiet": quiet,
        "input_bits": [Rect(row_x + i * cell, input_row_y, cell, half) for i in range(4)],
        "fiducials": [
            Rect(grid, grid, cell, cell),
            Rect(right, grid, cell, cell),
            Rect(grid, bottom, cell, cell),
            Rect(right, bottom, cell, cell),
        ],
        "readout": Rect(readout_x, bottom, right - grid - readout_x, cell),
    }


def gray_decode(gray):
    value = gray
    shift = 1
    while shift < 32:
        value ^= value >> shift
        shift <<= 1
    return value


# ---------------------------------------------------------------------------------------------------------------------
# Video input


def probe_capture_fps(path):
    """The real capture rate of the video, which slow motion files keep apart from their playback rate."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None

    info = json.loads(out)
    tags = info.get("format", {}).get("tags", {})
    # Android slow motion: playback at 30 fps, capture rate in a tag
    for key, value in tags.items():
        if key.lower() == "com.android.capture.fps":
            return float(value), "com.android.capture.fps tag"

    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            num, den = stream.get("avg_frame_rate", "0/1").split("/")
            if float(den) > 0:
                return float(num) / float(den), "average frame rate"
    return None, None


class Canvas:
    """
    The stream's pixel grid, extended half a stream to the left and right so a marker in pillarbox bars still lands
    on it, at about 8 canvas pixels per pattern cell.
    """

    def __init__(self, width, height, layout):
        self.scale = 8 / layout["cell"]
        self.x0 = -width / 2
        self.width = int(round(2 * width * self.scale))
        self.height = int(round(height * self.scale))

    def to_canvas(self, x, y):
        return (x - self.x0) * self.scale, y * self.scale

    def from_canvas(self, u, v):
        return u / self.scale + self.x0, v / self.scale

    def slices(self, rect):
        """Canvas array slices covering a rectangle given in stream pixels."""
        u0, v0 = self.to_canvas(rect.x, rect.y)
        u1, v1 = self.to_canvas(rect.x + rect.w, rect.y + rect.h)
        u0, v0 = int(np.floor(u0)), int(np.floor(v0))
        u1, v1 = max(int(np.ceil(u1)), u0 + 1), max(int(np.ceil(v1)), v0 + 1)
        return slice(max(v0, 0), max(v1, 1)), slice(max(u0, 0), max(u1, 1))


def find_fiducials(gray):
    """
    Find the four corner bullseyes: a white square holding a black ring holding a white center, which shows up as a
    contour with a child and a grandchild. Returns their centers as top left, top right, bottom left, bottom right.
    """
    background = float(np.median(gray))
    bright = float(np.percentile(gray, 99.8))
    if bright - background < 20:
        return None
    _, binary = cv2.threshold(gray, (background + bright) / 2, 255, cv2.THRESH_BINARY)
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return None
    hierarchy = hierarchy[0]

    candidates = []
    for i, contour in enumerate(contours):
        child = hierarchy[i][2]
        if child < 0:
            continue
        grandchild = hierarchy[child][2]
        if grandchild < 0:
            continue
        area = cv2.contourArea(contour)
        if area < 40:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if not 0.5 < w / h < 2:
            continue
        child_ratio = cv2.contourArea(contours[child]) / area
        grandchild_ratio = cv2.contourArea(contours[grandchild]) / area
        if not (0.25 < child_ratio < 0.9 and 0.005 < grandchild_ratio < 0.25):
            continue
        m = cv2.moments(contour)
        candidates.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))

    if len(candidates) < 4:
        return None
    points = np.array(candidates)
    sums = points[:, 0] + points[:, 1]
    diffs = points[:, 0] - points[:, 1]
    corners = [points[np.argmin(sums)], points[np.argmax(diffs)], points[np.argmin(diffs)], points[np.argmax(sums)]]
    if len({(round(p[0]), round(p[1])) for p in corners}) < 4:
        return None
    return np.array(corners, dtype=np.float32)


def read_video(path, canvas, layout, max_frames=None):
    """
    Resample every frame onto the canvas. Returns the stack of canvas frames, which frames are valid (false before the
    fiducials were first found), the number of frames where the fiducials were found, and a reference frame with its
    transform for drawing.
    """
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        sys.exit(f"Could not open {path}")

    canvas_fiducials = np.array([canvas.to_canvas(*f.center) for f in layout["fiducials"]], dtype=np.float32)
    frames = []
    valid = []
    found = 0
    transform = None
    reference = None
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    while max_frames is None or len(frames) < max_frames:
        ok, image = capture.read()
        if not ok:
            break
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (gray.shape[1] // 2, gray.shape[0] // 2), interpolation=cv2.INTER_AREA)

        corners = find_fiducials(small)
        if corners is not None:
            found += 1
            # Canvas pixel -> half resolution camera pixel. A held transform covers frames where detection fails.
            transform = cv2.getPerspectiveTransform(canvas_fiducials, corners)

        if transform is None:
            frames.append(np.zeros((canvas.height, canvas.width), dtype=np.uint8))
            valid.append(False)
        else:
            # Each canvas pixel spans several camera pixels, so average them first.
            blurred = cv2.blur(small, (3, 3))
            warped = cv2.warpPerspective(
                blurred,
                transform,
                (canvas.width, canvas.height),
                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            frames.append(warped)
            valid.append(True)

        if reference is None and corners is not None and len(frames) >= min(total // 2, 120):
            reference = (cv2.cvtColor(small, cv2.COLOR_GRAY2BGR), transform.copy(), len(frames) - 1)

        if len(frames) % 500 == 0:
            print(f"  read {len(frames)}{f'/{total}' if total else ''} frames", file=sys.stderr)

    capture.release()
    if not frames:
        sys.exit("The video has no frames")
    return np.stack(frames), np.array(valid), found, reference


# ---------------------------------------------------------------------------------------------------------------------
# Signal analysis


def fill_gaps(values):
    """Carry the last good value over NaN gaps (and the first one back over a leading gap)."""
    values = values.copy()
    good = np.flatnonzero(~np.isnan(values))
    if good.size == 0:
        return values
    values[: good[0]] = values[good[0]]
    for i in range(good[0] + 1, values.size):
        if np.isnan(values[i]):
            values[i] = values[i - 1]
    return values


def find_marker(stack, valid, canvas, layout):
    """
    Find the client's marker: outside the host pattern, the region whose brightness varies most over time, sized
    roughly like the trigger square. Returns its rectangle in stream pixels, or None.
    """
    usable = stack[valid]
    if usable.shape[0] < 2:
        return None
    spread = usable[:: max(1, usable.shape[0] // 400)].astype(np.float32).std(axis=0)

    # Mask out the host pattern's own changing parts.
    mask = np.ones_like(spread, dtype=bool)
    for rect in [layout["quiet"], layout["readout"], *layout["fiducials"]]:
        grown = Rect(rect.x - layout["cell"] / 4, rect.y - layout["cell"] / 4, rect.w + layout["cell"] / 2, rect.h + layout["cell"] / 2)
        rows, cols = canvas.slices(grown)
        mask[rows, cols] = False

    trigger_rows, trigger_cols = canvas.slices(layout["trigger"].inset(0.6, 0.6))
    trigger_spread = float(np.median(spread[trigger_rows, trigger_cols]))
    if trigger_spread <= 0:
        return None

    binary = ((spread > trigger_spread * 0.5) & mask).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=4)
    trigger_area = (layout["trigger"].w * canvas.scale) * (layout["trigger"].h * canvas.scale)

    best = None
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area < trigger_area * 0.25 or not 0.5 < w / h < 2:
            continue
        fill = area / (w * h)
        strength = float(np.median(spread[labels == label]))
        score = strength * fill * min(area / trigger_area, trigger_area / area)
        if best is None or score > best[0]:
            best = (score, x, y, w, h)

    if best is None:
        return None
    _, x, y, w, h = best
    sx, sy = canvas.from_canvas(x, y)
    return Rect(sx, sy, w / canvas.scale, h / canvas.scale)


def region_series(stack, valid, canvas, rect):
    rows, cols = canvas.slices(rect)
    values = stack[:, rows, cols].mean(axis=(1, 2))
    values[~valid] = np.nan
    return fill_gaps(values)


@dataclass
class Flip:
    """One flip of a square, timed at the halfway crossing."""

    time: float  # seconds
    rising: bool
    ramp: float  # seconds from 10 % to 90 % of the step
    frame: int  # camera frame just after the crossing


def crossing(values, start, end, level, rising):
    """Fractional frame index where values first cross level in the given direction, searching [start, end)."""
    for k in range(max(start, 0), min(end, values.size - 1)):
        a, b = values[k], values[k + 1]
        if (rising and a < level <= b) or (not rising and a > level >= b):
            return k + (level - a) / (b - a) if b != a else float(k)
    return None


def find_flips(values, fps, min_step):
    """Find each flip between the square's two levels, timed at the halfway crossing."""
    low, high = np.percentile(values, 5), np.percentile(values, 95)
    if high - low < min_step:
        return [], (low, high)
    mid = (low + high) / 2
    band = (high - low) * 0.25

    flips = []
    state = values[0] > mid
    for i in range(1, values.size):
        if state and values[i] < mid - band:
            rising = False
        elif not state and values[i] > mid + band:
            rising = True
        else:
            continue
        state = rising

        # Step back to where it crossed the middle, then time it against the levels either side of the step.
        j = i
        while j > 0 and ((values[j - 1] > mid) == rising):
            j -= 1
        before = values[max(0, j - 12) : max(1, j - 3)]
        after = values[i + 3 : i + 12]
        if before.size == 0 or after.size == 0:
            continue
        pre, post = float(np.median(before)), float(np.median(after))
        if abs(post - pre) < min_step:
            continue

        level = (pre + post) / 2
        frame = crossing(values, j - 4, i + 4, level, rising)
        t10 = crossing(values, j - 8, i + 8, pre + (post - pre) * 0.1, rising)
        t90 = crossing(values, j - 8, i + 8, pre + (post - pre) * 0.9, rising)
        if frame is None:
            continue
        ramp = (t90 - t10) / fps if t10 is not None and t90 is not None else float("nan")
        flips.append(Flip(frame / fps, rising, ramp, int(np.ceil(frame))))
    return flips, (low, high)


def read_input_seq(stack, canvas, layout, frame):
    """Decode the host's Gray-coded input number shown in a frame (mod 16), judging bits against the quiet zone."""
    frame = min(frame, stack.shape[0] - 1)
    quiet = layout["quiet"]
    reference = Rect(quiet.x + layout["cell"] / 8, quiet.y + quiet.h / 3, layout["cell"] / 4, quiet.h / 3)
    rows, cols = canvas.slices(reference)
    gray_level = float(stack[frame, rows, cols].mean())
    code = 0
    for bit in layout["input_bits"]:
        rows, cols = canvas.slices(bit.inset(0.5, 0.5))
        code = (code << 1) | int(float(stack[frame, rows, cols].mean()) > gray_level)
    return gray_decode(code)


def describe(values):
    values = np.asarray(values)
    if values.size == 0:
        return "none"
    return (
        f"n={values.size:<3d} median {np.median(values):6.2f}  mean {values.mean():6.2f}  sd {values.std():5.2f}  "
        f"p10 {np.percentile(values, 10):6.2f}  p90 {np.percentile(values, 90):6.2f}  "
        f"min {values.min():6.2f}  max {values.max():6.2f} ms"
    )


# ---------------------------------------------------------------------------------------------------------------------
# Debug images


def draw_regions(path, reference, canvas, regions):
    image, transform, index = reference
    for name, rect, color in regions:
        corners = [(rect.x, rect.y), (rect.x + rect.w, rect.y), (rect.x + rect.w, rect.y + rect.h), (rect.x, rect.y + rect.h)]
        points = np.array([[canvas.to_canvas(*c) for c in corners]], dtype=np.float32)
        projected = cv2.perspectiveTransform(points, transform)[0].astype(np.int32)
        cv2.polylines(image, [projected], True, color, 2)
        cv2.putText(image, name, tuple(int(v) for v in projected[0] - [0, 6]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    cv2.putText(image, f"frame {index}", (10, image.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    cv2.imwrite(str(path), image)


def draw_trace(path, series, flips, pairs, fps):
    """Plot both squares' brightness over the whole video, with each flip marked and each pair joined."""
    frames = series[0][1].size
    width = int(min(max(frames * 2, 1200), 8000))
    lane = 160
    image = np.full((lane * len(series) + 40, width, 3), 255, np.uint8)
    x_of = lambda t: int(t * fps / frames * (width - 1))

    for row, (name, values, color) in enumerate(series):
        top = row * lane + 10
        low, high = np.percentile(values, 1), np.percentile(values, 99)
        norm = (values - low) / max(high - low, 1e-6)
        points = np.array([[int(i / frames * (width - 1)), int(top + (1 - np.clip(n, -0.1, 1.1)) * (lane - 30))] for i, n in enumerate(norm)], np.int32)
        cv2.polylines(image, [points], False, color, 1)
        cv2.putText(image, name, (5, top + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        for flip in flips[row]:
            cv2.line(image, (x_of(flip.time), top), (x_of(flip.time), top + lane - 30), (180, 180, 180), 1)

    for local, host, _ in pairs:
        cv2.line(image, (x_of(local.time), lane - 20), (x_of(host.time), lane + 10), (0, 160, 0), 2)
    for second in range(int(frames / fps) + 1):
        x = x_of(second)
        cv2.putText(image, f"{second}s", (x + 2, image.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)
    cv2.imwrite(str(path), image)


# ---------------------------------------------------------------------------------------------------------------------


def parse_size(text):
    try:
        width, height = (int(v) for v in text.lower().split("x"))
        return width, height
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, got {text!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", type=Path)
    parser.add_argument("--stream", type=parse_size, required=True, help="stream resolution, e.g. 2560x1440")
    parser.add_argument("--fps", type=float, help="camera capture rate (default: read from the file)")
    parser.add_argument("--max-frames", type=int, help="only analyse the first N frames")
    parser.add_argument("--out", type=Path, help="directory for the CSV and debug images (default: next to the video)")
    args = parser.parse_args()

    fps, fps_source = (args.fps, "--fps") if args.fps else probe_capture_fps(args.video)
    if not fps:
        sys.exit("Could not tell the capture rate; pass --fps")
    if fps < 100:
        print(f"warning: capture rate is only {fps:.0f} fps ({fps_source}); pass --fps if that is the playback rate", file=sys.stderr)

    width, height = args.stream
    layout = compute_layout(width, height)
    canvas = Canvas(width, height, layout)
    out_dir = args.out or args.video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / args.video.stem

    print(f"Reading {args.video.name} at {fps:g} fps ({fps_source})", file=sys.stderr)
    stack, valid, found, reference = read_video(args.video, canvas, layout, args.max_frames)
    frames = stack.shape[0]
    if found == 0:
        sys.exit("The corner bullseyes were not found in any frame. Is the whole pattern in shot?")

    marker = find_marker(stack, valid, canvas, layout)
    if marker is None:
        sys.exit("Could not find Moonlight's latency marker. Is it on (Ctrl+Alt+Shift+T) and were keys pressed?")

    # Sample a strip through the middle rows of each square, clear of their edges.
    # When the marker is on the trigger's rows (the client display is as tall as the stream), sample exactly the
    # same rows, so both strips are scanned out at the same moment.
    trigger_strip = layout["trigger"].inset(0.6, 0.25)
    marker_strip = marker.inset(0.6, 0.25)
    same_rows = abs(marker.center[1] - layout["trigger"].center[1]) < layout["cell"] / 2
    if same_rows:
        marker_strip = Rect(marker_strip.x, trigger_strip.y, marker_strip.w, trigger_strip.h)
    host = region_series(stack, valid, canvas, trigger_strip)
    local = region_series(stack, valid, canvas, marker_strip)

    min_step = 20
    local_flips, local_levels = find_flips(local, fps, min_step)
    host_flips, host_levels = find_flips(host, fps, min_step)

    # Pair each marker flip with the first trigger flip after it and before the next marker flip.
    pairs = []
    missed = []
    matched_hosts = set()
    for i, flip in enumerate(local_flips):
        limit = local_flips[i + 1].time if i + 1 < len(local_flips) else float("inf")
        match = next((h for h in host_flips if flip.time < h.time < limit), None)
        if match is None:
            missed.append(flip)
            continue
        matched_hosts.add(id(match))
        seq = read_input_seq(stack, canvas, layout, match.frame + 6)
        pairs.append((flip, match, seq))
    extra_hosts = [h for h in host_flips if id(h) not in matched_hosts and local_flips and h.time > local_flips[0].time]

    latencies = [(h.time - l.time) * 1000 for l, h, _ in pairs]
    same_direction = [(h.time - l.time) * 1000 for l, h, _ in pairs if l.rising == h.rising]
    opposite_direction = [(h.time - l.time) * 1000 for l, h, _ in pairs if l.rising != h.rising]
    # The input number should go up by one per pair. Builds before the Gray code fix showed the low bits of the full
    # count's Gray code, which repeat a value at every wrap and then count back down, so steps of 0 and -1 are
    # accepted too. A dropped or merged input still shows up as a jump of two or more.
    skipped_seq = [i for i in range(1, len(pairs)) if (pairs[i][2] - pairs[i - 1][2]) % 16 not in (0, 1, 15)]

    # Pairs far from the others with the same edges, e.g. a marker that showed up late because the client had no new
    # frame to draw it on.
    outliers = []
    for marker_rising in (True, False):
        group = [(i, (h.time - l.time) * 1000) for i, (l, h, _) in enumerate(pairs) if l.rising == marker_rising]
        if len(group) < 4:
            continue
        values = np.array([v for _, v in group])
        median = np.median(values)
        spread = max(3 * 1.4826 * np.median(np.abs(values - median)), 1.5)
        outliers += [(i, v, median) for i, v in group if abs(v - median) > spread]

    # Outputs
    csv_path = stem.with_name(stem.name + ".latency.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["input", "local_ms", "local_edge", "host_ms", "host_edge", "latency_ms", "host_input_seq", "local_ramp_ms", "host_ramp_ms"])
        for i, (l, h, seq) in enumerate(pairs):
            writer.writerow([
                i + 1,
                f"{l.time * 1000:.2f}",
                "rise" if l.rising else "fall",
                f"{h.time * 1000:.2f}",
                "rise" if h.rising else "fall",
                f"{(h.time - l.time) * 1000:.2f}",
                seq,
                f"{l.ramp * 1000:.2f}",
                f"{h.ramp * 1000:.2f}",
            ])

    regions_path = stem.with_name(stem.name + ".regions.png")
    trace_path = stem.with_name(stem.name + ".trace.png")
    if reference is not None:
        draw_regions(regions_path, reference, canvas, [
            ("trigger", trigger_strip, (0, 0, 255)),
            ("marker", marker_strip, (0, 200, 0)),
            ("marker area", marker, (0, 120, 0)),
            *[(f"bit{i}", b.inset(0.5, 0.5), (255, 0, 0)) for i, b in enumerate(layout["input_bits"])],
        ])
    draw_trace(trace_path, [("marker (client)", local, (0, 140, 0)), ("trigger (stream)", host, (0, 0, 200))], [local_flips, host_flips], pairs, fps)

    # Report
    print()
    print(f"Video:       {frames} frames at {fps:g} fps = {frames / fps:.2f} s; stream {width}x{height}")
    print(f"Bullseyes:   found in {found}/{frames} frames")
    print(f"Marker:      x {marker.x:.0f} y {marker.y:.0f} size {marker.w:.0f}x{marker.h:.0f} in stream pixels "
          f"(trigger is at y {layout['trigger'].y} size {layout['trigger'].w}); "
          f"{'sampled on the same rows as the trigger' if same_rows else 'NOT on the trigger rows, so scanout timing differs'}")
    print(f"Flips:       marker {len(local_flips)}, trigger {len(host_flips)}, paired {len(pairs)}, "
          f"marker flips with no answer {len(missed)}, unpaired trigger flips {len(extra_hosts)}")
    print()
    print("Latency, marker flip to streamed trigger flip (halfway crossings):")
    print(f"  all          {describe(latencies)}")
    if opposite_direction:
        print(f"  same edge    {describe(same_direction)}")
        print(f"  opposite     {describe(opposite_direction)}")
    rising = [(h.time - l.time) * 1000 for l, h, _ in pairs if l.rising]
    falling = [(h.time - l.time) * 1000 for l, h, _ in pairs if not l.rising]
    print(f"  marker rise  {describe(rising)}")
    print(f"  marker fall  {describe(falling)}")
    if opposite_direction and rising and falling:
        # Opposite edges: one kind of pair reads long by the display's rise/fall difference and the other reads
        # short by the same amount, so their average cancels it.
        rise_median, fall_median = np.median(rising), np.median(falling)
        print(f"  balanced     {(rise_median + fall_median) / 2:6.2f} ms  (average of the rise and fall medians, which "
              f"cancels the display's rise/fall difference of {abs(rise_median - fall_median) / 2:.2f} ms each way)")
    print()

    def ramp(flips, rising_edge):
        values = [f.ramp * 1000 for f in flips if f.rising == rising_edge and not np.isnan(f.ramp)]
        return f"{np.median(values):.1f} ms" if values else "-"

    print(f"Edge length, 10-90 %: marker rise {ramp(local_flips, True)} fall {ramp(local_flips, False)}; "
          f"trigger rise {ramp(host_flips, True)} fall {ramp(host_flips, False)}")

    if opposite_direction:
        print()
        print(f"Note: in {len(opposite_direction)} of {len(pairs)} pairs the squares flipped in opposite directions "
              "(one went black to white while the other went white to black), so the display's rise and fall times "
              "differ between them. To line them up, quit the test app with Esc and relaunch it, then toggle the "
              "marker off and on before pressing keys.")
    if skipped_seq:
        print(f"Note: the host's input number jumped at {len(skipped_seq)} pair(s) "
              f"(e.g. input {skipped_seq[0] + 1}): an input was dropped or two arrived in one frame.")
    if outliers:
        listed = ", ".join(f"input {i + 1} ({v:.2f} ms vs {m:.2f})" for i, v, m in sorted(outliers))
        print(f"Note: {len(outliers)} outlier(s): {listed}. A short one usually means the marker was drawn late, "
              "because the client had no new frame to draw it on; a long one, a slow frame somewhere on the path.")
    if missed:
        print(f"Note: {len(missed)} marker flips had no trigger flip before the next marker flip, first at "
              f"{missed[0].time:.3f} s.")

    print()
    print(f"Wrote {csv_path.name}, {regions_path.name} (check the boxes sit on the squares) and {trace_path.name} "
          f"in {out_dir}")


if __name__ == "__main__":
    main()
