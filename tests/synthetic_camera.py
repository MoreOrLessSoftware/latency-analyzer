"""
Make a synthetic Latency Cam recording of the latency-test pattern, with known latencies, for end to end tests.

A host presents frames at a steady rate, each reaching the simulated client display a known time later; the display
responds to each change exponentially; a rolling shutter camera at 240 fps films it with its clock running at a
different rate and offset from the host's. Writes VIDEO.mp4, VIDEO.camera.json and the host's two logs, and returns
the true present to photon time of every host frame.
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))

from analyze_video import compute_layout  # noqa: E402

QPC_FREQUENCY = 10_000_000
START_QPC = 1_190_869_555_167


def gray_code(value):
    return value ^ (value >> 1)


def render_pattern(width, height, layout, frame, seq):
    """The test pattern for one host frame, as latency-test draws it with --no-noise (without the clock digits)."""
    image = np.full((height, width), 0.5, np.float32)
    readout = layout["readout"]
    image[int(readout.y):int(readout.y + readout.h), int(readout.x):int(readout.x + readout.w)] = 0
    for rect in layout["fiducials"]:
        x0, y0, size = int(rect.x), int(rect.y), int(rect.w)
        ys, xs = np.mgrid[0:size, 0:size]
        ring = np.maximum(np.abs(xs - size // 2), np.abs(ys - size // 2)) * 8 // size
        image[y0:y0 + size, x0:x0 + size] = np.where((ring == 0) | (ring >= 3), 1.0, 0.0)
    trigger = layout["trigger"]
    image[int(trigger.y):int(trigger.y + trigger.h), int(trigger.x):int(trigger.x + trigger.w)] = float(seq & 1)
    frame_gray = gray_code(frame & 255)
    for i, rect in enumerate(layout["frame_bits"]):
        image[int(rect.y):int(rect.y + rect.h), int(rect.x):int(rect.x + rect.w)] = float((frame_gray >> (7 - i)) & 1)
    input_gray = gray_code(seq & 15)
    for i, rect in enumerate(layout["input_bits"]):
        image[int(rect.y):int(rect.y + rect.h), int(rect.x):int(rect.x + rect.w)] = float((input_gray >> (3 - i)) & 1)
    return image


def make_recording(out_dir, seconds=3.0, stream=(1280, 720), host_fps=120.0, camera=(1280, 720), base_latency_ms=40.0,
                   tau_ms=4.0, tau_fall_ms=None, latency_jitter_ms=4.0, exposure_ms=4.0, exposure_samples=4, drift_ppm=-18.0,
                   seed=1):
    """
    tau_ms is the display's time constant going to white, and tau_fall_ms going to black (the same by default).
    Returns the true present to halfway time of every host frame for each direction (truth_ms for white,
    truth_fall_ms for black), the flips and the present times.
    """
    tau_fall_ms = tau_ms if tau_fall_ms is None else tau_fall_ms
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    width, height = stream
    layout = compute_layout(width, height)

    # Host: frames at a steady rate with a little jitter, flips every 150-400 ms
    count = int(seconds * host_fps) + 40
    present = 0.5 + np.arange(count) / host_fps + rng.normal(0, 0.0002, count)  # host seconds since start_qpc
    flips = []
    t = present[0] + 0.2
    while t < present[-1]:
        flips.append(int(np.searchsorted(present, t)))
        t += rng.uniform(0.15, 0.4)
    seq_of_frame = np.zeros(count, np.int64)
    for k, frame in enumerate(flips):
        seq_of_frame[frame:] = k + 1
    # Present to the display starting to change, then to the halfway point each way
    latency = base_latency_ms / 1000 + rng.uniform(0, latency_jitter_ms / 1000, count)
    appear = present + latency
    truth = (latency + tau_ms / 1000 * np.log(2)) * 1000
    truth_fall = (latency + tau_fall_ms / 1000 * np.log(2)) * 1000

    header = (f"# latency-test\n# start_utc=2026-11-01T00:00:00.000Z start_filetime=0 start_qpc={START_QPC} "
              f"qpc_frequency={QPC_FREQUENCY}\n# width={width} height={height} refresh_hz=120 fps={host_fps} vsync=false "
              "tearing=true noise=0\n# auto=true auto_min_ms=150 auto_max_ms=400 time_server=port 48100\n"
              "# Times are microseconds of QueryPerformanceCounter since start_qpc.\n")
    with open(out_dir / "host.csv", "w") as f:
        f.write(header + "seq,source,detail,input_us,render_us,present_call_us,present_return_us,frame,still_queued\n")
        for k, frame in enumerate(flips):
            us = present[frame] * 1e6
            f.write(f"{k + 1},a,0,{us - 5:.1f},{us - 3:.1f},{us:.1f},{us + 200:.1f},{frame},0\n")
    with open(out_dir / "host.frames.csv", "w") as f:
        f.write(header + "frame,render_us,present_call_us,present_return_us,input_seq\n")
        for n in range(count):
            us = present[n] * 1e6
            f.write(f"{n},{us - 3:.1f},{us:.1f},{us + 200:.1f},{seq_of_frame[n]}\n")

    # Camera: phone clock = host clock stretched by the drift, plus an offset
    zero = START_QPC / QPC_FREQUENCY
    phone_of = lambda host_s: (host_s + zero) * (1 + drift_ppm * 1e-6) * 1e9 + 5e14
    host_of = lambda phone_ns: (phone_ns - 5e14) / 1e9 / (1 + drift_ppm * 1e-6) - zero
    cam_w, cam_h = camera
    frames = int((seconds - 0.2) * 240)
    first = present[0] + 0.1
    exposure_ns, skew_ns = int(exposure_ms * 1e6), 4_542_720
    active = [0, 0, 4080, 3072]
    crop = [0, 0, 4080, 3072]
    shown_height = min(crop[3], crop[2] * cam_h / cam_w)
    row_offset, row_span = (crop[3] - shown_height) / 2 / active[3], shown_height / active[3]

    # The display fills most of the shot, without perspective
    screen = (int(cam_w * 0.06), int(cam_h * 0.06), int(cam_w * 0.88), int(cam_h * 0.88))
    patterns = {}

    def pattern(n):
        if n not in patterns:
            image = render_pattern(width, height, layout, n, int(seq_of_frame[n]))
            patterns[n] = cv2.resize(image, (screen[2], screen[3]), interpolation=cv2.INTER_AREA)
        return patterns[n]

    writer = cv2.VideoWriter(str(out_dir / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 240, (cam_w, cam_h))
    starts, captures = [], []
    screen_rows = np.arange(screen[3])
    for k in range(frames):
        start_phone = phone_of(first + k / 240) + rng.normal(0, 2000)
        starts.append(start_phone)
        captures.append([int(start_phone), exposure_ns, skew_ns, 33357008, *crop])
        camera_rows = screen[1] + screen_rows
        row_start = start_phone + skew_ns * (row_offset + row_span * camera_rows / cam_h)
        # The exposure averages what the display showed while it was open
        image = np.zeros((screen[3], screen[2]), np.float32)
        for sample in range(exposure_samples):
            row_host = host_of(row_start + exposure_ns * (sample + 0.5) / exposure_samples)
            # Each row shows every frame's exponential step since it appeared, at the time constant of its direction
            newest = np.searchsorted(appear, row_host.max())
            shown = None
            for n in range(max(newest - 8, 0), newest):
                current = pattern(n)
                if shown is None:
                    shown = current
                    continue
                age = np.maximum(row_host - appear[n], 0)[:, None]
                if tau_ms == tau_fall_ms:
                    weight = 1 - np.exp(-age / (tau_ms / 1000))  # one curve for every pixel: much quicker
                else:
                    weight = 1 - np.exp(-age / (np.where(current > shown, tau_ms, tau_fall_ms) / 1000))
                shown = current * weight + shown * (1 - weight)
            image += shown / exposure_samples
        frame_image = np.full((cam_h, cam_w), 0.08, np.float32)
        frame_image[screen[1]:screen[1] + screen[3], screen[0]:screen[0] + screen[2]] = 0.1 + 0.8 * image
        gray8 = np.clip(frame_image * 255, 0, 255).astype(np.uint8)
        writer.write(cv2.cvtColor(gray8, cv2.COLOR_GRAY2BGR))
    writer.release()

    # Clock sync exchanges every 50 ms with 2-4 ms round trips and occasional queuing
    samples = []
    t = first - 1.0
    while t < first + seconds + 1:
        up, down = 0.001 + rng.exponential(0.0005), 0.001 + rng.exponential(0.0005)
        t1 = phone_of(t)
        t2 = (t + up + zero) * QPC_FREQUENCY
        t3 = t2 + 30
        t4 = phone_of(t + up + down + 3e-6)
        samples.append([int(t1), int(t2), int(t3), int(t4)])
        t += 0.05

    video_pts_us = [int((s - 1_000_000_000) / 1000) for s in starts]  # CLOCK_MONOTONIC is 1 s behind
    timing = {
        "format": "ml-latency-camera", "version": 1, "video": "video.mp4", "device": "Synthetic", "camera_id": "0",
        "width": cam_w, "height": cam_h, "fps": 240, "timestamp_source": "REALTIME", "sensor_orientation": 90,
        "clock_fields": ["realtime_ns", "monotonic_ns", "uncertainty_ns"],
        "clocks_at_start": [2_000_000_000, 1_000_000_000, 500], "clocks_at_stop": [3_000_000_000, 2_000_000_000, 500],
        "video_pts_us": video_pts_us, "active_array": active,
        "capture_fields": ["sensor_timestamp_ns", "exposure_ns", "rolling_shutter_skew_ns", "frame_duration_ns",
                           "crop_left", "crop_top", "crop_width", "crop_height"],
        "captures": captures,
        "sync": {"host": "10.0.0.29", "qpc_frequency": QPC_FREQUENCY, "fields": ["t1", "t2", "t3", "t4"], "samples": samples},
    }
    (out_dir / "video.camera.json").write_text(json.dumps(timing))
    return {"truth_ms": truth, "truth_fall_ms": truth_fall, "flips": flips, "present": present}


if __name__ == "__main__":
    result = make_recording(sys.argv[1] if len(sys.argv) > 1 else "synthetic")
    print(f"{len(result['flips'])} flips, median truth {np.median(result['truth_ms']):.2f} ms")
