# ml-latency-analyzer

Measures a game stream's latency and pacing with a camera pointed at the client display. **Latency Cam**, an Android
app, films the client display at 240 fps with every camera frame timed on the host's clock, and `latency-test.exe` on
the host shows a test pattern and logs when it presented each frame. The analysis times every trigger flip and every
new frame reaching the client's screen, for:

- **host present → photon:** the time from the game handing its frame to Windows on the host to the client display
  showing it, for every flip and every frame
- **pacing:** how evenly the client showed the host's frames, which shows whether VRR pacing is working
- with Sunshine's frame log and Moonlight's pacing trace, the **stages in between**, including Moonlight's own
  "capture to present"

| File | What it is |
|---|---|
| `app/` | The host test app, built as `latency-test.exe`: the test pattern, its logs, the clock sync and upload server |
| `android/` | Latency Cam (see [android/README.md](android/README.md)) |
| `analyzer/analyze_video.py` | The analyzer's entry point: finds the pattern in the video; bundled as `latency-analyzer.exe` |
| `analyzer/camera_mode.py`, `analyzer/camera_timing.py` | Times what the camera saw on the host's clock |
| `analyzer/traces.py` | Joins the results with Sunshine's frame log and Moonlight's pacing trace |
| `analyzer/report.py` | The HTML report |
| `analyzer/build_exe.ps1`, `analyzer/requirements.txt` | Bundle the analyzer into `latency-analyzer.exe` with PyInstaller |
| `analyzer/time_server.py` | Stand-alone version of the app's clock sync server, for testing Latency Cam without the app |
| `tests/` | Unit tests (C++ with GoogleTest, Python with unittest), including an end to end run on a synthetic recording with known latencies |

## What you need

- A Sunshine host with `latency-test.exe` and `latency-analyzer.exe` (see [Build](#build) and
  [Deploy to a host](#deploy-to-a-host)).
- A Moonlight client. For the [stage breakdown](#breaking-it-down-with-sunshines-and-moonlights-logs), Sunshine
  writing its `sunshine-frames-*.csv` and Moonlight its pacing trace (`ML_PACING_TRACE`).
- An Android phone that films 1920x1080 at 240 fps through the camera API, with Latency Cam installed. The Pixel 10 Pro
  qualifies; [android/README.md](android/README.md) says how to check others.

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

## The test app

`latency-test.exe` options:

| Option | Effect |
|---|---|
| `--vsync` | Starts with v-sync on: waits for vertical blank. By default the app presents straight away, with tearing, so it adds no wait of its own. **V** toggles it while running. |
| `--noise <0..1>` | Background noise amplitude, default 0.5. The noise keeps the encoder near its bitrate; 0 turns it off. |
| `--no-noise` | Flat gray background, so frames stay small (same as `--noise 0`). |
| `--fps <n>` | Background update rate. The default is the display's refresh rate. **F** changes it while running. |
| `--log <path>` | CSV log of each flip, timed with the host's QPC. The default is `%TEMP%\sunshine_latency_test.csv`. Every frame's render and present times go to the same name ending `.frames.csv`. |
| `--no-auto` | Starts without automatic flips. By default the trigger flips on its own every 150–400 ms, at random, as well as on key presses; automatic flips land on the next scheduled frame, so the frame rate stays steady. **A** toggles them while running. |
| `--auto-interval <min>-<max>` | The interval range for automatic flips in ms, for example `100-300`. |
| `--time-port <n>` | Port answering Latency Cam, default 48100: clock sync over UDP and uploads over TCP. |
| `--no-time-server` | Doesn't answer clock sync requests or accept uploads. |
| `--results <dir>` | Where Latency Cam uploads and their analysis go. The default is a `results` folder next to `latency-test.exe`, or `Latency Cam` in Documents when that can't be written. |
| `--analyzer <path>`, `--python <path>` | The `latency-analyzer.exe` or `analyze_video.py`, and the Python, to run on uploads, if not found next to the app. |
| `--windowed` | Runs in a 1280x720 window instead of fullscreen. |

The screen shows:

- noise
- the trigger square in the middle, with a row of bits above it (the Gray-coded frame number) and below it (the
  input number)
- a bullseye in each corner
- a readout band at the bottom with the host clock and the time since the last flip
- a status band at the top: the frame rate the app aims for and where it came from, the display's refresh rate,
  v-sync or tearing, the frame rate actually presented over the last second, the automatic flips and the Latency Cam
  port

By default the app presents without v-sync, with tearing. It redraws the background on a timer at `--fps`, or at the
display's refresh rate without it. A key press is answered straight away with an extra frame; automatic flips are
shown on the next scheduled frame instead, which keeps the cadence steady for measuring pacing.

Keys, on the host or through Moonlight; none of them counts as a flip, and each change is noted in both logs
(`# auto=`, `# vsync=`, `# fps=`):

| Key | Does |
|---|---|
| **A** | Toggles automatic flips. |
| **V** | Toggles v-sync. With v-sync on and no `--fps`, the display sets the pace: each present waits for the display to take the previous frame, so frames follow its refresh instead of the app's timer. That adds about a frame of latency. Each frame's line in `.frames.csv` records whether v-sync was on. |
| **F** | Types a new frame rate in the status band: digits, `.` and Backspace edit it, Enter applies it (nothing typed goes back to the display's refresh rate) and Esc cancels. A rate a little under the display's maximum, such as 97 at 100 Hz, keeps a VRR display following the app's frames. |
| **Esc** | Quits (or cancels typing a frame rate). While Latency Cam is uploading, an analysis is running or the phone hasn't downloaded the report yet, the test pattern closes and a small window shows the progress; it closes by itself, and the app with it, when everything is done (it gives the phone up to 15 s to start an analysis or download a report). **Close anyway** quits at once: an analysis then carries on in the background and still saves its report on the host, but the phone can't fetch it. |

## Measure

1. **Host:** stream `latency-test.exe`. The trigger flips on its own, and the app answers Latency Cam on port 48100.
2. **Phone:** open Latency Cam on the same network. Wait for the sync line to turn green, frame the whole client
   display (all four bullseyes, the trigger and the frame number row), enter a test name and record for 10–30 s.
3. **Results:** after **Stop**, choose **Upload & analyze**. The host analyses the recording and the phone shows the
   headline numbers, and keeps a copy of the report, which **Open report** shows without the host.

On the host, each recording gets its own folder, `results\NAME\`, holding the video, its timing file, copies of the
host's logs and everything the analysis wrote: `NAME.report.html`, `NAME.summary.json`, `NAME.flips.csv`,
`NAME.frames.csv` and `NAME.regions.png`.

Tips:

- Hold the phone steady or use a stand. Handheld works too, because every frame is re-aligned on the bullseyes.
- Moonlight's stats overlay (Ctrl+Alt+Shift+S) can stay on. It hides the top left bullseye, and the analyzer
  estimates that corner from the other three.
- Avoid glare on the display, and fill as much of the shot with it as you can: the frame number patches are small.
- Keep `latency-test.exe` running until the upload and analysis are done. It starts its logs afresh each time it
  runs, so a recording uploaded after a restart has nothing to match; the analysis says so.

### Analyse again

To analyse an uploaded recording again, for example after adding Sunshine's and Moonlight's logs to its folder, give
the analyzer just its results folder:

```
latency-analyzer.exe results\NAME
```

Or run `latency-analyzer.exe` with no arguments, or double-click it: it lists the results folders it finds (next to
itself, in the current folder and in `Documents\Latency Cam`), newest first, with each one's result and which logs it
holds, and asks which to analyse again.

It finds `NAME.mp4`, its timing file, the host log copy and any logs in the folder, takes the stream's size from the
test app's log and its frame rate from what the test app presented while filming, and writes the report back into the
folder.

A recording made elsewhere works the same way: put `NAME.mp4` and `NAME.camera.json` in one folder and pass the host
log from the run (by default the `NAME.host.csv` next to the video, else `%TEMP%\sunshine_latency_test.csv`, with its
`.frames.csv` next to it):

```
python analyzer/analyze_video.py NAME.mp4 --host-log sunshine_latency_test.csv --report
```

| Option | Meaning |
|---|---|
| `--stream WxH` | Stream resolution. The default is the test app's, from its log. |
| `--stream-fps N` | Stream frame rate, for frame gridlines. The default is the rate the test app presented at while filming. |
| `--host-log PATH` | latency-test.exe's log from the filmed run. |
| `--camera-timing PATH` | Latency Cam's timing file, if not `NAME.camera.json` next to the video. |
| `--sunshine-frames PATH`, `--moonlight-trace PATH` | Sunshine's and Moonlight's logs, if not in the video's folder. |
| `--report` | Also write the HTML report (always, for a results folder). |
| `--out DIR` | Output folder. The default is the video's folder. |
| `--max-frames N` | Analyse only the start of the video. |

## Read the results

"Present" alone is ambiguous here, so the analysis never uses it:

- **host present**: the game (the test app) handing its frame to Windows on the host.
- **client present**: Moonlight handing the decoded frame to Windows on the client. Moonlight's statistic **capture
  to present** runs from capture to this point, before the client display has shown anything.

```
host present ─▶ capture ─────────────▶ client present ─────────▶ photon
     │◀────▶│◀─ Moonlight: capture to present ─▶│◀──────────▶│
     │◀──────────── camera: host present → photon ───────────▶│
```

- **Host present → photon, flips:** from the test app handing the flip to Windows on the host, to the trigger
  crossing halfway on the client display, for every flip. It includes everything: the capture wait, encoding, the
  network, decoding, the client's pacing and the client display's own delay.
- **Host present → photon, every frame:** the same for every host frame, timed by the frame number patch above the
  trigger that changes with it. Only one patch changes per frame, because the number is Gray coded.
- **To white / to black:** displays reach halfway sooner one way than the other, often by 2–3 ms, so latencies are
  shown for each direction.
- **Pacing:** for each pair of consecutive frames, the client's display interval minus the host's present interval,
  corrected for each kind of edge's own delay. With VRR pacing working, these pile up at 0; at a fixed refresh rate
  they spread by the refresh interval. **Uneven** counts the pairs off by more than a quarter of a frame, and
  **frames not seen** the host frames that never showed (or whose patches couldn't be read).
- **At the display's maximum refresh rate,** VRR can't follow the app's frames, so each frame waits for the display's
  scan to come round, and the result depends on where in the scan the app's timer happens to land. Run a little under
  it (F, then 97 at 100 Hz) to measure with VRR in play.

### Breaking it down with Sunshine's and Moonlight's logs

Put Sunshine's `sunshine-frames-*.csv` and Moonlight's `Moonlight-pacing-*.csv` from the filmed run in the recording's
folder (or pass `--sunshine-frames` and `--moonlight-trace`), and the analysis follows every frame the camera timed
through them. The report and summary get a **Pipeline** section with each stage's time:

| Stage | From |
|---|---|
| host present → capture | the test app and Sunshine, both on the host's QueryPerformanceCounter |
| capture → encoded, capture → sent | Sunshine |
| capture → first packet | Moonlight's capture to present less first packet → client present: the host sending the first of the frame, and the network |
| first packet → last packet → decoder hands it over → GPU decode finished → client present | Moonlight, on the client's clock |
| capture → client present (Moonlight's capture to present) | Moonlight, through its clock sync: the same figure as its statistics |
| client present → photon | the camera's capture → photon less Moonlight's capture to present: the client display's own part |
| capture → photon, host present → photon | the camera (and Sunshine) |

Moonlight's capture to present, and so the rows built on it, need a Moonlight build whose pacing trace has the
`capture_to_present_us` column. With it, the headline also shows **capture → client present** and **client present
→ photon** next to **capture → photon**, and the three add up frame by frame.

The client's clock is offset from the host's by an unknown amount, so no stage spans both except through Moonlight's
clock sync or the camera. The spreads are also compared frame by frame: client present minus capture shows how steady
the whole path was, and photon minus client present how closely the camera and Moonlight agree on each frame.

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

## The upload API

`latency-test.exe` serves these on TCP port 48100 while it runs (see `app/http.h`):

| Request | Does |
|---|---|
| `PUT /recordings/NAME.mp4`, `PUT /recordings/NAME.camera.json` | Saves a recording's files in `results\NAME\` |
| `POST /recordings/NAME/analyze` | Runs the analyzer on them with the app's own log, resolution and frame rate |
| `GET /recordings/NAME/status` | `analyzing`, `done` with the summary, or `failed` with the analyzer's last lines |
| `GET /recordings/NAME/report` | The HTML report |

Names may only use letters, digits, `.`, `_` and `-`. The server only runs while the test app does, and
`--no-time-server` turns it off. Anyone on the network can upload to it while it runs.

## Troubleshooting

| Message | Fix |
|---|---|
| `The corner bullseyes were not found in any frame` | Get all four corners in shot. Reduce glare. Make sure the stream is the latency-test app. |
| `The recording ended ... before this host log began` | The test app was restarted between recording and analysis. Record again, keeping it running until the analysis is done. |
| `No Latency Cam timing file` | The analyzer only reads Latency Cam recordings, which come with `NAME.camera.json`. |
| Many frames not seen | The frame number patches were too small or blurred to read: frame the display larger. |
| Boxes off the squares in `regions.png` | Check that `--stream` matches the test app's resolution (the default takes it from its log). |
