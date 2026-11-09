# Plan: Android test pattern app

Status: not started. Saved 2026-11-04 for later.

## Goal

An Android app that shows the same test pattern as `latency-test.exe`, so Latency Cam (on a second phone) can measure
present -> photon on an Android device directly, without streaming. Keep it minimal: no upload server, no noise, no
status band, no keyboard/mouse input, no in-app toggles.

Rough size: 2–3 days, about 500–800 new lines.

## Setup

- Two devices on the same Wi-Fi: the **pattern device** runs the new app; the **camera phone** runs Latency Cam pointed
  at it. (A phone can't film its own screen.)
- Analysis runs on a PC, because nothing on the phones runs the Python analyzer.

## What the pattern app must do

### 1. Draw the pattern

Bullseyes, trigger patch, frame bits, input bits and the clock / time-since-input readouts. Leave out the noise
(flat mid-gray background) and the info band text.

- Layout: `app/pattern.h` is platform independent (block grid, `layout_t`, Gray codes, readout sizing). An NDK
  (C++) app can include it as is. A Java/Kotlin app would have to port it and risk drifting from the original.
  **Recommendation: NDK + GLES**, so `pattern.h` and `clock_sync.h` are shared with `latency-test.exe`.
- Drawing: port the HLSL pixel shader in `app/main.cpp` to a GLSL ES fragment shader with the same constants
  (frame, input_seq, clock_ms, since_input_ms, rectangles, digit positions). Set noise to 0.
- Full screen, immersive mode, keep screen on, fixed orientation.

### 2. Flip the trigger on a timer

The only source of flips, since there is no input. Reuse the automatic flip scheduling from `main.cpp`
(`schedule_auto_flip`: random interval between auto_min_ms and auto_max_ms, and the flip waits for the next scheduled
frame so the cadence stays steady). Each flip gets a new input sequence number, source `a`.

### 3. Write the same logs as latency-test

The analyzer (`analyzer/camera_timing.py`, `camera_mode.py`) reads two CSVs and needs the header fields:

- Input log `NAME.csv`: `seq,source,detail,input_us,render_us,present_call_us,present_return_us,frame,still_queued`
- Frames log `NAME.frames.csv`: `frame,render_us,present_call_us,present_return_us,input_seq,vsync`
- Header (both files), as written in `main.cpp`:
  ```
  # latency-test
  # start_utc=... start_filetime=... start_qpc=... qpc_frequency=...
  # width=... height=... refresh_hz=... fps=... fps_source=... rtss_limit=0 vsync=1 tearing=0 noise=0
  # auto=1 auto_min_ms=... auto_max_ms=... time_server=1
  # Times are microseconds of QueryPerformanceCounter since start_qpc.
  ```
- Clock: use `CLOCK_MONOTONIC` in nanoseconds as the "QPC", with `qpc_frequency=1000000000`.
- Present timing: time around `eglSwapBuffers` for `present_call_us` / `present_return_us`.
- `start_filetime` is Windows-specific; check what the analyzer uses it for and write an equivalent
  (100 ns ticks since 1601) or make the analyzer tolerate its absence.

### 4. Answer clock sync

Latency Cam stamps camera frames on the pattern device's clock through UDP port 48100 (`app/clock_sync.h`,
`android/.../ClockSync.java`):

- Request, 16 bytes: magic "MLCS", sequence (u32), t1 (i64).
- Reply, 40 bytes: magic "MLCR", sequence, t1 echoed, t2 and t3 (i64, clock ticks), ticks per second.

Answer with `CLOCK_MONOTONIC` ns and frequency 1e9 (must match `qpc_frequency` in the logs; `camera_mode.py`
checks this). About 50 lines on a background thread. This is the clock sync only, not the upload server.

### 5. Fixed refresh rate

Phones with adaptive (LTPO, 1–120 Hz) panels change rate on their own. Request a fixed rate with
`Surface.setFrameRate` / `ANativeWindow_setFrameRate` or a preferred display mode, and log the rate actually used as
`refresh_hz` (and `fps`, `fps_source=display`). Pace frames with `Choreographer` / v-sync.

## Getting results analysed

1. Latency Cam: record, then don't upload (or let the upload fail). The recording and its `.camera.json` are saved in
   `Download/LatencyCam` on the camera phone.
2. Pattern app: write its logs somewhere reachable (e.g. `Download/LatencyPattern` via MediaStore, or app storage
   pulled with `adb pull`).
3. On the PC, put them in one folder named like an upload: `NAME.mp4`, `NAME.camera.json`, `NAME.host.csv`,
   `NAME.host.frames.csv` (check the exact names `analyze_video.py` / `camera_timing.py` look for), then run
   `latency-analyzer.exe <folder>`.

Possible follow-up: a small helper script to assemble the folder from the two devices' files.

## Optional: break down with frame timestamps

`EGL_ANDROID_get_frame_timestamps` reports per frame when the compositor latched it and when the display started
showing it (`EGL_DISPLAY_PRESENT_TIME_ANDROID`). Logging those would split the result into app -> compositor ->
display start -> photon, which `latency-test.exe` can't do on Windows. Needs extra CSV columns and analyzer support,
so do it after the basic version works.

## Things to watch

- **Small screen:** frame-bit patches get physically tiny; the camera has to be close. `pattern.h` already drops to a
  16 px grid on small screens, but check the patches are big enough in millimetres.
- **OLED PWM flicker:** can band at 240 fps. Run at full brightness.
- **Analyzer assumptions:** grep the analyzer for Windows/streaming-specific assumptions (Sunshine/Moonlight stages,
  `rtss_limit`, `tearing`, the "host present" wording in reports) and make sure missing values don't break it.

## Where it lives

A separate small app module (or a second launcher activity) in the existing `android/` Gradle project, so it reuses
the build setup. NDK/CMake for the native part, including `../app/pattern.h` and `../app/clock_sync.h`.

## Order of work

1. NDK skeleton: full-screen GLES surface, fixed refresh rate.
2. Draw the pattern from `pattern.h` with timed flips.
3. Clock sync server; check Latency Cam's sync line turns green.
4. Logs in latency-test's format.
5. Manual analysis of one run on the PC; fix any analyzer assumptions.
6. Optional: frame timestamps breakdown.
