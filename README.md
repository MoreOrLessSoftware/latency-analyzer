# ml-latency-analyzer

Measures a stream's latency with a camera pointed at the client display. There are two ways:

- **[Latency Cam](#latency-cam-present-to-photon-and-pacing)** (recommended): an Android app films at 240 fps with every
  frame timed on the host's clock. It measures the time from the host presenting each frame to it reaching the client
  display, and how evenly the client shows the host's frames, with no client changes.
- **Moonlight's latency marker**, filmed with any slow motion camera, described below. It measures from the client
  drawing its marker, so the client's own frame pacing is part of every sample.

The marker test needs no clock sync and no footage of a finger pressing a key, because both events it times appear on
the same screen:

- **Marker:** Moonlight's latency marker is a square drawn by the client. It flips between black and white when the
  client sends a key or mouse press, and reaches the screen with the next frame the client draws.
- **Trigger:** the `latency-test` app on the host draws a square in the middle of the screen. It flips when the
  press arrives, so the flip only shows on the client after a full trip through capture, encode, network, decode and
  display.

A slow motion video of the client display shows both flips. Any lag in the display itself cancels out, because both
squares are on the same screen.

The time between the flips is not the whole latency from the key press, because the marker isn't on screen the
moment the key is pressed either. It waits for the next frame the client draws, which with Moonlight's frame pacing
is typically 5–20 ms, and longer at lower frame rates. During an automatic test (Space), Moonlight records that
delay for every press in a marker log, and with it (`--marker-log`) the analyzer adds it on and reports the time from
the key press to the trigger being presented.

| File | What it is |
|---|---|
| `app/main.cpp`, `app/pattern.h` | The host test app, built as `latency-test.exe` |
| `analyzer/analyze_video.py` | Finds both flips in the video and reports the latency |
| `analyzer/report.py` | The HTML report written by `analyze_video.py --report` |
| `analyzer/analyze.bat` | Drag and drop wrapper for Windows |
| `tests/test_pattern.cpp` | Unit tests for the test pattern layout and options |
| `tests/test_analyzer.py` | Unit tests for the analyzer and report helpers |
| `analyzer/camera_mode.py`, `analyzer/camera_timing.py` | Analysis of Latency Cam videos |
| `analyzer/traces.py` | Joins Latency Cam results with Sunshine's frame log and Moonlight's pacing trace |
| `tests/test_camera_timing.py`, `tests/synthetic_camera.py` | Tests for it, including an end to end run on a synthetic recording with known latencies |
| `analyzer/build_exe.ps1`, `analyzer/requirements.txt` | Bundle the analyzer into `latency-analyzer.exe` with PyInstaller |
| `analyzer/time_server.py` | Stand-alone version of the app's clock sync server, for testing Latency Cam without the app |
| `android/` | Latency Cam, an Android app that records 240 fps video with every frame timed on the host's clock (see [android/README.md](android/README.md)) |

## Latency Cam: present to photon and pacing

1. **Host:** stream `latency-test.exe`. The trigger flips on its own (A toggles it), and the app answers Latency Cam on
   port 48100: clock sync over UDP and uploads over TCP. Allow it through the firewall for both (see
   [Deploy to a host](#deploy-to-a-host)). Uploads are analysed by `latency-analyzer.exe` next to `latency-test.exe`,
   which needs nothing else installed; without it, by `analyze_video.py` with Python, `numpy` and
   `opencv-python-headless`. `--analyzer` and `--python` point elsewhere.
2. **Phone:** open Latency Cam ([android/README.md](android/README.md)) on the same network. Wait for the sync line to
   turn green, frame the whole client display, enter a test name and record for 10–30 s.
3. **Results:** after **Stop** and **Upload & analyze**, the host analyses the recording and the phone shows the
   headline numbers. The phone also keeps a copy of the report, which **Open report** shows without the host. On the
   host, each recording gets its own folder, `results\NAME\`, holding the
   video, its timing file, copies of the host's logs and everything the analysis wrote. `results` sits next to
   `latency-test.exe`, or is `Documents\Latency Cam` when that can't be written (under Program Files, for example).
   `--results` changes it.

To analyse a recording by hand, put `NAME.mp4` and `NAME.camera.json` in one folder and pass the host log from the
run (by default `%TEMP%\sunshine_latency_test.csv`, with its `.frames.csv` next to it):

```
python analyzer/analyze_video.py NAME.mp4 --stream 3840x2160 --stream-fps 117 --host-log sunshine_latency_test.csv --report
```

The analyzer switches to this mode when `NAME.camera.json` sits next to the video.

### The upload API

`latency-test.exe` serves these on TCP port 48100 while it runs (see `app/http.h`):

| Request | Does |
|---|---|
| `PUT /recordings/NAME.mp4`, `PUT /recordings/NAME.camera.json` | Saves a recording's files in the results folder |
| `POST /recordings/NAME/analyze` | Runs the analyzer on them with the app's own log, resolution and frame rate |
| `GET /recordings/NAME/status` | `analyzing`, `done` with the summary, or `failed` with the analyzer's last lines |
| `GET /recordings/NAME/report` | The HTML report |

Names may only use letters, digits, `.`, `_` and `-`. The server only runs while the test app does, and
`--no-time-server` turns it off. Anyone on the network can upload to it while it runs.

It reports:

- **Present to photon, flips:** from the host's present call to the trigger crossing halfway on the client display,
  for every flip.
- **Present to photon, every frame:** the same for every host frame, timed by the frame number patch above the
  trigger that changes with it. Only one patch changes per frame, because the number is Gray coded.
- **Pacing:** for each pair of consecutive frames, the client's display interval minus the host's present interval.
  With VRR pacing working, these pile up at 0; at a fixed refresh rate they spread by the refresh interval.
  **Uneven** counts the pairs off by more than a quarter of a frame, and **frames not seen** the host frames that
  never showed (or whose patches couldn't be read).

Writes `NAME.flips.csv`, `NAME.frames.csv`, `NAME.regions.png` and, with `--report`, `NAME.report.html`.

Present to photon includes everything after the present call: the capture wait, encoding, the network, decoding, the
client's pacing and the display's own delay.

### Breaking it down with Sunshine's and Moonlight's logs

Put Sunshine's `sunshine-frames-*.csv` and Moonlight's `Moonlight-pacing-*.csv` from the filmed run in the recording's
folder (or pass `--sunshine-frames` and `--moonlight-trace`), and the analysis follows every frame the camera timed
through them. The report and summary get a **Pipeline** section with each stage's time:

| Stage | From |
|---|---|
| app present call → Sunshine capture | the test app and Sunshine, both on the host's QueryPerformanceCounter |
| capture → encoded, capture → sent | Sunshine |
| first packet → last packet → decoder hands it over → GPU decode finished → present | Moonlight, on the client's clock |
| app present → photon, capture → photon | the camera (and Sunshine) |

The client's clock is offset from the host's by an unknown amount, so no single stage spans both. Instead the spreads
are compared frame by frame: Moonlight's present minus Sunshine's capture shows how steady the whole path was, and the
camera's photon minus Moonlight's present how closely the camera and Moonlight agree on each frame.

Either log works alone. With only Sunshine's, the client stages are missing. With only Moonlight's, its rows are
matched to the test app's frames by lining up the frame-to-frame jitter of their timestamps; the host stages are
missing, and the report warns when the match is uncertain (very even presents leave little to line up). When there
are several of either, the ones that match the most frames are used.

### Accuracy

- **Clock sync:** the phone assumes its requests and the replies take equally long. A one-sided delay shifts every
  time by up to half the fastest round trip, the same way for all of them; the report notes round trips over 3 ms.
  The scatter around the fit shows the noise.
- **Camera:** each row's time comes from the frame's sensor timestamp, the rolling shutter skew and the crop, at the
  middle of the exposure. On a synthetic recording with a realistic display edge (time constant 4 ms) and a 4 ms
  exposure, results read 0.4 ms late with 0.13 ms spread: averaging over the exposure and interpolating between
  camera frames 4.2 ms apart both lag a curved edge slightly.
- **Rolling shutter direction:** the analyzer assumes the sensor reads the video's rows top to bottom as stored. Film
  the host's own monitor once to check: its flips should land a few ms after the host's present times.

## What you need

- A Sunshine host with `latency-test.exe` (see [Build](#build)).
- A Moonlight client with the latency marker: moonlight-qt, branch `latency-marker`. The marker works with the
  D3D11VA, DXVA2, Vulkan (placebo) and SDL renderers. For the time from the key press, the client must also log the
  marker's delay, which only branch `mlsoft-custom2` does so far.
- A phone that films slow motion at 240 fps or more. Some phones save the real capture rate in the video file;
  Android does, in the `com.android.capture.fps` tag. When it doesn't, pass `--fps`.
- Python 3 with `numpy` and `opencv-python-headless` (`python -m pip install numpy opencv-python-headless`).
  You also need `ffprobe` on `PATH` to read the capture rate.

## Build

On Windows, build with MSYS2 UCRT64 (`cmake`, `ninja` and `gcc` from the `mingw-w64-ucrt-x86_64-` packages):

```
cmake -S . -B cmake-build-release -G Ninja -DCMAKE_BUILD_TYPE=Release
ninja -C cmake-build-release
cmake-build-release/test_latency.exe
python -m unittest discover -s tests
```

`latency-test.exe` is linked statically, so it can be copied to the host on its own. The unit tests download
GoogleTest at configure time; pass `-DBUILD_TESTS=OFF` to skip them.

To bundle the analyzer, with Python, numpy and OpenCV, into a single `latency-analyzer.exe` (about 65 MB):

```
ninja -C cmake-build-release latency-analyzer
```

It runs `analyzer/build_exe.ps1`, which needs Python 3 with the `py` launcher and network access the first time. It
installs `analyzer/requirements.txt` and PyInstaller into a virtual environment in the build folder, leaving the
system's Python packages alone. It isn't part of the default build because it takes about a minute.

## Deploy to a host

1. Build both, then install them into one folder:

   ```
   ninja -C cmake-build-release latency-analyzer
   cmake --install cmake-build-release --prefix C:\Tools\latency-test
   ```

   The folder holds `latency-test.exe`, `latency-analyzer.exe`, the analyzer scripts and this README. Only the two
   exes are needed on a host; copy the folder to it as it is.
2. On the host, allow the app through the firewall before the first stream, from an elevated prompt, so Windows
   doesn't ask in the middle of one:

   ```
   netsh advfirewall firewall add rule name="latency-test" dir=in action=allow program="C:\Tools\latency-test\latency-test.exe" enable=yes
   ```
3. In Sunshine, add an app whose command is the full path to `latency-test.exe`.

Results go to `results` in that folder, so keep it somewhere the streaming user can write to (not Program Files), or
pass `--results`.

## Set up

1. In Sunshine's Applications page, add an app whose command is the full path to `latency-test.exe`. Its options:

   | Option | Effect |
   |---|---|
   | `--vsync` | Starts with v-sync on: waits for vertical blank. By default the app presents straight away, with tearing, so it adds no wait of its own. **V** toggles it while running. |
   | `--noise <0..1>` | Background noise amplitude, default 0.5. The noise keeps the encoder near its bitrate; 0 turns it off. |
   | `--no-noise` | Flat gray background, so frames stay small (same as `--noise 0`). |
   | `--fps <n>` | Background update rate. The default is the display's refresh rate. |
   | `--log <path>` | CSV log of each input and the frame that answered it, timed with the host's QPC. The default is `%TEMP%\sunshine_latency_test.csv`. Every frame's render and present times go to the same name ending `.frames.csv`. |
   | `--no-auto` | Starts without automatic flips. By default the trigger flips on its own every 150–400 ms, at random, as well as on key presses; automatic flips land on the next scheduled frame, so the frame rate stays steady. **A** toggles them while running. Use `--no-auto` for the marker test. |
   | `--auto-interval <min>-<max>` | The interval range for automatic flips in ms, for example `100-300`. |
   | `--time-port <n>` | Port answering Latency Cam, default 48100: clock sync over UDP and uploads over TCP. Allow `latency-test.exe` through the firewall when asked. |
   | `--no-time-server` | Doesn't answer clock sync requests or accept uploads. |
   | `--results <dir>` | Where Latency Cam uploads and their analysis go. The default is a `results` folder next to `latency-test.exe`, or `Latency Cam` in Documents when that can't be written. |
   | `--analyzer <path>`, `--python <path>` | The `analyze_video.py` and Python to run on uploads, if not found automatically. |
   | `--windowed` | Runs in a 1280x720 window instead of fullscreen. |

2. Stream the app. The screen shows:
   - noise
   - the trigger square in the middle, with a row of bits above it (frame number) and below it (input number)
   - a bullseye in each corner
   - a readout band at the bottom with the host clock and the time since the last input
   - a status band at the top: the frame rate the app aims for and where it came from, the display's refresh rate,
     v-sync or tearing, the frame rate actually presented over the last second, how the trigger flips and the Latency
     Cam port

   By default the app presents without v-sync, with tearing. It redraws the background on a timer at `--fps`, or at
   the display's refresh rate without it. A key press is answered straight away with an extra frame, so the presented
   rate runs a little above the target while keys are pressed. Automatic flips are shown on the next scheduled frame
   instead, which keeps the cadence steady for measuring pacing.

   Keys, on the host or through Moonlight; none of them counts as a flip, and each change is noted in both logs
   (`# auto=`, `# vsync=`, `# fps=`):

   | Key | Does |
   |---|---|
   | **A** | Toggles automatic flips. |
   | **V** | Toggles v-sync. With v-sync on and no `--fps`, the display sets the pace: each present waits for the display to take the previous frame, so frames follow its refresh instead of the app's timer. That adds about a frame of latency. Each frame's line in `.frames.csv` records whether v-sync was on. |
   | **F** | Types a new frame rate in the status band: digits, `.` and Backspace edit it, Enter applies it (nothing typed goes back to the display's refresh rate) and Esc cancels. A rate just off the display's, such as 97 at 100 Hz, makes frames drift through the display's scan, which shows the scan wait apart from the display's own delay. |
   | **Esc** | Quits (or cancels typing a frame rate). While Latency Cam is uploading, an analysis is running or the phone hasn't downloaded the report yet, the test pattern closes and a small window shows the progress; it closes by itself, and the app with it, when everything is done (it gives the phone up to 15 s to start an analysis or download a report). **Close anyway** quits at once: an analysis then carries on in the background and still saves its report on the host, but the phone can't fetch it. |
3. For the marker test, automatic flips must be off: start the app with `--no-auto`, or press A before the next
   step. Otherwise the trigger flips without a press and the pairing goes wrong.
4. Press **Ctrl+Alt+Shift+T** in Moonlight to turn the marker on. It appears a quarter of the way across the screen,
   on the same rows as the trigger, with a timer under it showing the time since it last flipped.

## Record

1. Put the phone on a stand or hold it steady. Frame the whole client display: all four corner bullseyes, the
   trigger and the marker must be in shot. Handheld works too, because every frame is re-aligned on the bullseyes.
2. Start slow motion recording at 240 fps or more.
3. Press **Space** in Moonlight. The client sends 30 presses of F24, a key that does nothing on the host, at random
   intervals of 300–700 ms. The timer shows its progress (`n/30`). Pressing Space again stops early. You can also
   press keys yourself; Esc quits the test app.
4. Stop recording after the last press. Name the file after what you were testing, for example
   `mine-amf-copy-pacing-4ktv.mp4`, because the report takes its title from the file name.

### Tips

- Both squares should start black. If the marker and trigger flip in opposite directions, the display's
  rise and fall times get mixed into the result, and the analyzer adds a note. To realign them:
  1. Quit the app with Esc and start it again.
  2. Toggle the marker off and on.
- The stats overlay (Ctrl+Alt+Shift+S) can stay on. It hides the top left bullseye, and the analyzer estimates that
  corner from the other three.
- Avoid filming with V-Sync off on the client. Tearing smears the squares and causes early or late outliers.
- Turn off auto exposure changes if the phone allows it, and avoid glare on the display.

## Analyse

### Drag and drop (Windows)

Drop one or more videos on `analyze.bat`. It asks three questions:

- **Stream resolution:** what Moonlight streamed at, for example `3840x2160` or `2560x1440`. This is the stream
  size, not the client display size.
- **Stream frame rate:** for example `120`. It labels the results in stream frames. Enter `0` if you don't know it.
- **Marker log:** where Moonlight's latency test CSVs are, to add the marker's own delay (see
  [Moonlight's marker log](#moonlights-marker-log)). The default, `%TEMP%`, is where Moonlight writes them on
  Windows, so it works as is when you analyse on the client. Otherwise drag in a CSV or a folder of them, or enter
  `-` to skip it.

Pressing Enter keeps the default shown in brackets. Set the `LATENCY_STREAM`, `LATENCY_STREAM_FPS` and
`LATENCY_MARKER_LOG` environment variables to change those defaults.

The results go to a `latency-results` folder next to each video. Each report opens in your browser when its
video is done.

### Command line

```
python analyze_video.py VIDEO --stream 3840x2160 [--stream-fps 120] [--fps 240] [--marker-log PATH] [--report] [--out DIR] [--max-frames N]
```

| Option | Meaning |
|---|---|
| `--stream WxH` | Stream resolution (required). The trigger's position is computed from it. |
| `--stream-fps N` | Stream frame rate; the report then shows latency in frames and draws a gridline per frame. |
| `--fps N` | Camera capture rate, if the file doesn't record it. The analyzer warns when the rate it reads is under 100 fps, because that is usually the playback rate of a slow motion video. |
| `--marker-log PATH` | Moonlight's latency test CSV, a folder of them, or Moonlight's log, to add the marker's own delay and report the time from the key press. With a folder, the CSV that matches the video is used. `--moonlight-log` does the same. |
| `--report` | Also write the HTML report. |
| `--out DIR` | Output folder. The default is the video's folder. |
| `--max-frames N` | Analyse only the start of the video. |

### Output

| File | Contents |
|---|---|
| `VIDEO.report.html` | One self-contained page with these sections: <ul><li>headline figures</li><li>a chart of every press</li><li>groups by frame count</li><li>statistics</li><li>notes</li><li>both debug images</li></ul> |
| `VIDEO.latency.csv` | One row per paired press, with these columns: <ul><li>flip times</li><li>edges</li><li>latency</li><li>host input number</li><li>10–90 % edge lengths</li><li>with the marker log: the test's press number, the marker's delay to being presented and to its drawing starting, and the key press to the trigger being presented</li></ul> |
| `VIDEO.regions.png` | A camera frame with the sampled regions outlined. Check that the boxes sit on the squares. |
| `VIDEO.trace.png` | Both squares' brightness over the whole video, with flips marked and each pair joined. |

The console prints the same summary as plain text.

### Moonlight's marker log

Each automatic test (Space) gets a CSV next to Moonlight's log, `Moonlight-latency-test-<time>.csv`. On Windows that
is `%TEMP%`, or Moonlight's own folder for a portable install. It gets a row for each of the test's presses, once the
first frame showing its marker flip is presented, numbered as the timer counts them:

```
press,press_s,present_ms,draw_ms
7,1234.567890,11.840,4.710
```

`press_s` is when the key was pressed, on Moonlight's clock, and `present_ms` and `draw_ms` are how long after it the
frame was presented and started drawing. The same lines go to Moonlight's log, `Moonlight-<time>.log`, which the
analyzer reads too. Keys you press yourself flip the marker but aren't logged. Moonlight keeps the latest 20 CSVs.

The analyzer doesn't rely on the press numbers, so a CSV or log may hold presses that weren't filmed: it matches the
logged presses to the filmed flips by the pattern of gaps between them. Only the test's presses get the key press to
presented figure.

## Read the results

- **Key press to presented,** with the marker log, is the time from the key press to the trigger's flip being
  presented by the client. It adds the marker's logged delay to the latency below. Like the latency, it leaves out
  the display's own delay: add the display's input lag for input to photon.
- **Latency** is timed from the marker's flip to the trigger's flip, each at the point where its brightness
  crosses halfway. Camera exposure, display response and scanout blur each flip over several frames, but they
  blur both squares alike, so the halfway points stay comparable. The marker is sampled on the same rows as the
  trigger, so both are scanned out at the same moment.
- **Groups:** latencies usually cluster one stream frame apart: 8.33 ms at 120 fps, 16.7 ms at 60 fps. The share of
  presses in each group tells you more than the mean. For example, "26 at 1 frame and 4 at 2 frames" means
  either that a frame is sometimes held back somewhere on the path, or that those presses' markers were presented
  early. With the marker log, the key press to presented chart tells the two apart.
- **Key press to presented chart:** with the marker log, each press is a bar from the key press, split into the
  marker's own delay (orange) and the marker to trigger latency (blue). A press with a short orange part and a
  long blue part landed at a different point in the stream's frame; only a tall bar overall is a slow press.
- **Outliers** are presses far from the median of presses with the same edge. With the marker log, they are judged
  by the time from the key press, so a long outlier is a slow frame somewhere on the path. Presses that only stand
  out from marker to trigger are listed in a separate note. Without the log, a short outlier usually means the
  marker was drawn late, because the client had no new frame to draw it on, and a long one is a slow frame. When
  most presses are late, the on-time ones get flagged as outliers instead.
- **Marker rise and fall:** the figures split by the direction the marker flipped. If these differ while both
  squares flip the same way, something other than the display is at work.
- **Edge length** is the display's 10–90 % transition time. It tells you about the panel, not the stream. Long or
  uneven rises are normal on TVs and don't change the latency, because both squares are affected alike.
- **Host input number jumps** mean an input was dropped, or two arrived in the same host frame.

## Troubleshooting

| Message | Fix |
|---|---|
| `The corner bullseyes were not found in any frame` | Get all four corners in shot. Reduce glare. Make sure the stream is the latency-test app. |
| `Could not find Moonlight's latency marker` | Turn the marker on (Ctrl+Alt+Shift+T) and make sure presses were sent while filming. |
| `Could not tell the capture rate` | Install `ffprobe`, or pass `--fps`. |
| `NOT on the trigger rows` | The client display isn't as tall as the stream, so the two squares scan out at different times. Match the client resolution to the stream. |
| Boxes off the squares in `regions.png` | Check that `--stream` matches what Moonlight streamed. |
