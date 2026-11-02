#!/usr/bin/env python3
"""
Measure streaming latency from a Latency Cam video of the latency-test pattern.

Latency Cam films the client display at 240 fps and times every camera frame on the host's QueryPerformanceCounter
clock, and latency-test.exe logs when it presented each frame on the same clock. This finds the pattern in every
camera frame and times each trigger flip and each new frame number reaching the client display, for the time from the
host present to the photon, and how evenly the client showed the host's frames (see camera_mode.py).

How it finds the pattern:
 - The four corner bullseyes are found in every frame, so the camera may be handheld. All four must be in view. They
   give a perspective transform from the stream's pixel grid to the camera image, and each frame is resampled onto
   that grid.
 - The trigger and frame number patches are placed by the same layout code as the app (pattern.h).
 - Each change is timed where the brightness crosses halfway between the levels before and after it, interpolated
   between camera frames.

Requires numpy and opencv-python (or opencv-python-headless).

Usage:
  python analyze_video.py VIDEO [--report]
  python analyze_video.py results/NAME          (analyse an uploaded recording again)
  python analyze_video.py                       (list the results folders and choose one)

See README.md next to this script for the whole procedure.
"""
import argparse
import json
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np



# ---------------------------------------------------------------------------------------------------------------------
# Test pattern layout, ported from app/pattern.h. Keep the two in sync.


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
        "frame_bits": [Rect(row_x + i * half, frame_row_y, half, half) for i in range(8)],
        "fiducials": [
            Rect(grid, grid, cell, cell),
            Rect(right, grid, cell, cell),
            Rect(grid, bottom, cell, cell),
            Rect(right, bottom, cell, cell),
        ],
        "readout": Rect(readout_x, bottom, right - grid - readout_x, cell),
        "info": Rect(readout_x, grid, right - grid - readout_x, min(max(frame_row_y - half - grid, 0), cell)),
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


class Canvas:
    """
    The stream's pixel grid, extended half a stream to the left and right, at about 8 canvas pixels per pattern
    cell.
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


def largest_child(contours, hierarchy, index):
    """
    The largest contour directly inside contour index, or -1. Specks of noise can come first in the hierarchy, so all
    of them are compared.
    """
    best, best_area = -1, 0.0
    child = hierarchy[index][2]
    while child >= 0:
        area = cv2.contourArea(contours[child])
        if area > best_area:
            best, best_area = child, area
        child = hierarchy[child][0]
    return best


def bullseye_centers(gray, threshold):
    """
    Centers of the bullseyes in a frame thresholded at the given level: a white square holding a black ring holding a
    white center, which shows up as a contour with a child and a grandchild of the right sizes.
    """
    _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]

    centers = []
    for i, contour in enumerate(contours):
        area = cv2.contourArea(contour)
        if area < 40:
            continue
        child = largest_child(contours, hierarchy, i)
        if child < 0:
            continue
        grandchild = largest_child(contours, hierarchy, child)
        if grandchild < 0:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if not 0.5 < w / h < 2:
            continue
        child_ratio = cv2.contourArea(contours[child]) / area
        grandchild_ratio = cv2.contourArea(contours[grandchild]) / area
        if not (0.25 < child_ratio < 0.9 and 0.005 < grandchild_ratio < 0.25):
            continue
        m = cv2.moments(contour)
        centers.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))
    return centers


# A quadrilateral's corners are taken for the display's when the parallelogram they would make is out by at most this
# fraction of their diagonals: more than a camera's perspective or a curved display bends it, less than a stray
# bullseye-like shape in the pattern
MAX_SKEW = 0.2


def corner_roles(points):
    """Order four points as top left, top right, bottom left, bottom right, or None when that's ambiguous."""
    sums = points[:, 0] + points[:, 1]
    diffs = points[:, 0] - points[:, 1]
    roles = [int(np.argmin(sums)), int(np.argmax(diffs)), int(np.argmin(diffs)), int(np.argmax(sums))]
    return roles if len(set(roles)) == 4 else None


def quad_area(corners):
    """Area of the quadrilateral top left, top right, bottom right, bottom left."""
    x, y = corners[[0, 1, 3, 2], 0], corners[[0, 1, 3, 2], 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def plausible_four(points):
    """
    The display's corners among four points, ordered as top left, top right, bottom left, bottom right, or None when
    they can't be: a stray point makes the shape far from a parallelogram.
    """
    roles = corner_roles(points)
    if roles is None:
        return None
    corners = points[roles]
    tl, tr, bl, br = corners
    diagonal = (np.linalg.norm(br - tl) + np.linalg.norm(tr - bl)) / 2
    if diagonal <= 0 or np.linalg.norm(tl + br - tr - bl) > MAX_SKEW * diagonal:
        return None
    return corners


def choose_corners(candidates):
    """
    The display's corners among candidate bullseyes, ordered as top left, top right, bottom left, bottom right: the
    largest plausible four, or None when no four are.
    """
    points = np.array(candidates, dtype=np.float32)
    best = None
    for combo in combinations(range(len(points)), 4):
        corners = plausible_four(points[list(combo)])
        if corners is not None and (best is None or quad_area(corners) > quad_area(best)):
            best = corners
    return best


def find_fiducials(gray):
    """
    Find the four corner bullseyes. Returns their centers as top left, top right, bottom left, bottom right, or None
    unless all four are found.
    """
    background = float(np.median(gray))
    bright = float(np.percentile(gray, 99.8))
    if bright - background < 20:
        return None

    # Halfway between the background and white usually works. With a short exposure or a dim corner of the display the
    # white squares can fall below it, so lower levels are tried too. When the display fills little of the shot, the
    # median is the dark room around it and the display's own gray clears halfway, merging the white squares into it,
    # so higher levels are tried as well. The brightest part of the frame is often the trigger, well above the
    # bullseyes, and each corner may need its own level, so what each level finds is pooled. A bullseye found again
    # nearby at another level is the same one; the first level's position is kept. Low levels can turn up stray
    # shapes too, so only corners that make a plausible display outline are used.
    candidates = []
    same = gray.shape[1] * 0.03
    for level in (0.5, 0.3, 0.7, 0.85, 0.15, 0.08):
        for point in bullseye_centers(gray, background + (bright - background) * level):
            if all(np.hypot(point[0] - c[0], point[1] - c[1]) > same for c in candidates):
                candidates.append(point)
        if len(candidates) >= 4:
            corners = choose_corners(candidates)
            if corners is not None:
                return corners
    return None


def read_video(path, canvas, layout, max_frames=None):
    """
    Resample every frame onto the canvas. Returns the stack of canvas frames, which frames are valid (false before the
    fiducials were first found), the number of frames where the fiducials were found, a reference frame with its transform for drawing, each frame's transform from canvas pixels to
    full resolution camera pixels (None before the fiducials were first found), and the camera frame size.
    """
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        sys.exit(f"Could not open {path}")

    canvas_fiducials = np.array([canvas.to_canvas(*f.center) for f in layout["fiducials"]], dtype=np.float32)
    frames = []
    valid = []
    transforms = []
    size = None
    found = 0
    transform = None
    reference = None
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    while max_frames is None or len(frames) < max_frames:
        ok, image = capture.read()
        if not ok:
            break
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        size = gray.shape[1], gray.shape[0]
        small = cv2.resize(gray, (gray.shape[1] // 2, gray.shape[0] // 2), interpolation=cv2.INTER_AREA)

        corners = find_fiducials(small)
        if corners is not None:
            found += 1
            # Canvas pixel -> half resolution camera pixel. A held transform covers frames where detection fails.
            transform = cv2.getPerspectiveTransform(canvas_fiducials, corners)

        # The same transform, to full resolution camera pixels
        full = None if transform is None else np.diag([gray.shape[1] / small.shape[1], gray.shape[0] / small.shape[0], 1.0]) @ transform
        transforms.append(full)
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

        # More often when the output goes to a file, which the test app reads for its progress bar
        if len(frames) % (500 if sys.stderr.isatty() else 100) == 0:
            print(f"  read {len(frames)}{f'/{total}' if total else ''} frames", file=sys.stderr)

    capture.release()
    if not frames:
        sys.exit("The video has no frames")
    return np.stack(frames), np.array(valid), found, reference, transforms, size


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


# ---------------------------------------------------------------------------------------------------------------------


def parse_size(text):
    try:
        width, height = (int(v) for v in text.lower().split("x"))
        return width, height
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, got {text!r}")


def find_recording(folder):
    """
    The recording in a results folder: NAME.mp4 in a folder named NAME, as uploads are saved, or else the only video
    with Latency Cam's timing file next to it.
    """
    named = folder / (folder.name + ".mp4")
    if named.is_file():
        return named
    videos = [v for v in sorted(folder.glob("*.mp4")) if v.with_name(v.stem + ".camera.json").is_file()]
    if len(videos) == 1:
        return videos[0]
    if not videos:
        sys.exit(f"No recording in {folder}: expected {named.name}, or a video with its .camera.json next to it")
    sys.exit(f"{folder} holds several recordings ({', '.join(v.name for v in videos)}); give the one to analyse")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", type=Path,
                        help="the video, or a results folder from an upload (results\\NAME) to analyse it again")
    parser.add_argument("--stream", type=parse_size,
                        help="stream resolution, e.g. 2560x1440 (default for Latency Cam videos: the test app's, from "
                             "its log)")
    parser.add_argument("--max-frames", type=int, help="only analyse the first N frames")
    parser.add_argument("--out", type=Path, help="directory for the CSV and debug images (default: next to the video)")
    parser.add_argument("--stream-fps", type=float,
                        help="stream frame rate, to express latencies in stream frames (default for Latency Cam videos: "
                             "the rate the test app presented at while filming)")
    parser.add_argument("--report", action="store_true", help="also write an HTML report, VIDEO.report.html")
    parser.add_argument("--camera-timing", type=Path,
                        help="Latency Cam's timing file (default: VIDEO.camera.json next to the video)")
    parser.add_argument("--host-log", type=Path,
                        help="latency-test.exe's CSV log from the filmed run, for Latency Cam videos "
                             "(default: VIDEO.host.csv next to the video, as uploads keep it, else "
                             "latency_test.csv in %%TEMP%%)")
    parser.add_argument("--sunshine-frames", type=Path,
                        help="Sunshine's sunshine-frames-*.csv from the filmed run (default: any in the video's or "
                             "output folder), to break Latency Cam results down by stage")
    parser.add_argument("--moonlight-trace", type=Path,
                        help="Moonlight's Moonlight-pacing-*.csv from the filmed run (default: any in the video's or "
                             "output folder)")
    args = parser.parse_args()
    if args.video.is_dir():
        # A results folder: analyse its recording again, writing the results back into it
        args.video = find_recording(args.video)
        args.report = True
    if not args.video.is_file():
        sys.exit(f"No such file: {args.video}")
    timing_path = args.camera_timing or args.video.with_name(args.video.stem + ".camera.json")
    if args.camera_timing and not timing_path.is_file():
        sys.exit(f"No such file: {timing_path}")
    if not timing_path.is_file():
        sys.exit(f"No Latency Cam timing file ({timing_path.name}) next to {args.video.name}: record with Latency Cam, "
                 "which saves one with every video")
    import camera_mode

    camera_mode.run(args, timing_path)


# ---------------------------------------------------------------------------------------------------------------------
# Run without arguments: pick a results folder to analyse again


def results_roots():
    """Where uploaded results may be: next to this program (or the bundled exe), the current folder, and Documents."""
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    roots = [here / "results", here.parent / "results", Path.cwd() / "results", Path.home() / "Documents" / "Latency Cam"]
    seen = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.append(resolved)
    return seen


def list_runs(roots):
    """Every run folder (one holding NAME.mp4 and NAME.camera.json) under the roots, newest first."""
    runs = []
    for root in roots:
        for folder in root.iterdir():
            if folder.is_dir() and (folder / f"{folder.name}.mp4").is_file() and (folder / f"{folder.name}.camera.json").is_file():
                runs.append(folder)
    return sorted(runs, key=lambda f: (f / f"{f.name}.mp4").stat().st_mtime, reverse=True)


def describe_run(folder):
    """One line about a run folder: when it was recorded, its result if analysed, and which logs it has."""
    import datetime

    when = datetime.datetime.fromtimestamp((folder / f"{folder.name}.mp4").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    result = "not analysed"
    summary = folder / f"{folder.name}.summary.json"
    if summary.is_file():
        try:
            frames = json.loads(summary.read_text(encoding="utf-8")).get("frames")
            result = f"host present -> photon {frames['median']:.1f} ms" if frames else "analysed, no frames read"
        except (ValueError, KeyError, TypeError):
            result = "analysed"
    logs = [label for label, pattern in (("Sunshine log", "sunshine-frames-*.csv"), ("Moonlight trace", "Moonlight-pacing-*.csv"))
            if any(folder.glob(pattern))]
    return f"{folder.name}  {when}  {result}" + (f"  [{', '.join(logs)}]" if logs else "")


def choose_run():
    """List the run folders and ask which to analyse again; the chosen folder, or None."""
    roots = results_roots()
    runs = list_runs(roots)
    if not runs:
        print("\nNo results folders found in: " + (", ".join(str(r) for r in roots) or "results next to this program"))
        return None
    print("\nResults to analyse again, newest first:")
    for number, folder in enumerate(runs, 1):
        print(f"  {number:2d}. {describe_run(folder)}")
    while True:
        try:
            answer = input("\nNumber to analyse again (Enter to quit): ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(runs):
            return runs[int(answer) - 1]
        print(f"Enter a number from 1 to {len(runs)}.")


def interactive():
    """Without arguments: show the usage, then offer the results folders to analyse again."""
    print("usage: latency-analyzer VIDEO_OR_RESULTS_FOLDER [options]   (--help lists the options)")
    folder = choose_run() if sys.stdin.isatty() else None
    if folder is not None:
        sys.argv = [sys.argv[0], str(folder)]
        try:
            main()
        except SystemExit as error:
            if error.code not in (None, 0):
                print(error.code if isinstance(error.code, str) else f"Stopped ({error.code})")
    # A window opened by double-clicking closes as soon as the program ends, so wait
    if sys.stdin.isatty():
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass


if __name__ == "__main__":
    if len(sys.argv) == 1:
        interactive()
    else:
        main()
