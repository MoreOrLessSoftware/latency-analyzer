# ML Latency Analyzer

Measures latency and frame pacing with a camera pointed at a display, either a Windows PC's own display or a game
stream's client display. **Latency Cam**, an Android app, films the display at 240 fps with every camera frame timed
on the clock of the PC running `latency-test.exe`, which shows a test pattern and logs when it presented each frame.
The analysis times every trigger flip and every new frame reaching the screen, for:

- **host present → photon:** the time from the game handing its frame to Windows to the display showing it, for every
  flip and every frame
- **pacing:** how evenly the display showed the game's frames, which shows whether VRR pacing is working

Without a stream, the test app runs on the PC whose display you film, and the result is that PC's own latency from
present to photon: its GPU, display settings and monitor. With a stream, the test app runs on the host and the camera
films the client, and the result covers the whole path, from the host's present through the stream to the client
display. It works with any streaming host that runs on Windows (Sunshine, for example) and any client, and needs
nothing from the streaming software itself. Builds of the host and client that log every frame can optionally break
the result [down by stage](#optional-breaking-it-down-by-stage).

To see what streaming itself adds, measure the client display on its own first, then stream at the same frame rate
and compare the two (see [Measuring a stream against a baseline](#measuring-a-stream-against-a-baseline)).

## Quick Start Latency Measurement

1. **Download** the latest release from the
   [Releases page](https://github.com/MoreOrLessSoftware/latency-analyzer/releases): `LatencyAnalyzer-VERSION-windows.zip`,
   which holds `latency-test.exe` and `latency-analyzer.exe`, and the Latency Cam app, `LatencyCam-VERSION.apk`.
2. **Unzip** the Windows zip into a folder you can write to, such as `C:\Tools\latency-test`, on the PC that will run
   the test (to measure a stream, that's the streaming host). Results are saved in a `results` folder there, so avoid
   Program Files.
3. **Install Latency Cam** on the phone: copy the APK over (or download it on the phone) and open it, allowing installs
   from that source when Android asks. The phone has to film 1920x1080 at 240 fps (see [What you need](#what-you-need)).
4. **Start the test app:** run `latency-test.exe`. The first time, Windows asks whether to allow it through the
   firewall: allow it on private networks, or the phone can't reach it. It opens fullscreen on a QR code.
   - To measure the PC's own display, run it on that PC.
   - To measure a stream, add `latency-test.exe` as an app in the streaming host (in Sunshine: Applications, Add New,
     Command) and start it from the client, so the QR code shows on the client display.
5. **Connect the phone:** with the phone on the same network as the PC, scan the QR code with the phone's camera. It
   opens Latency Cam with the PC's address filled in. Wait for the sync line to turn green.
6. **Record:** on the **Recording** tab, enter a test name and point the phone at the display so the whole QR code
   screen is in view, ideally with the phone on a stand. Once the view is steady, the PC switches to the test pattern,
   the phone counts down from 3 and records for 20 s. Keep all four corner bullseyes in shot until it stops.
7. **Analyse:** choose **Upload & analyze**. The test pattern shows the upload and analysis progress, and the phone
   switches to the **Results** tab with the headline numbers when it's done. **Open report** shows the full report.
   Keep `latency-test.exe` running until then.
8. **Finish:** press Esc on the PC to quit the test app. The recording and its report stay in `results\NAME` next to
   `latency-test.exe`, and the phone keeps a copy of the report.

To find out how much latency streaming adds rather than the total, repeat this with the test app running on the client
itself, at the same frame rate, and compare the two runs (see
[Measuring a stream against a baseline](#measuring-a-stream-against-a-baseline)).

## Screenshots & Sample Results

TODO

## Project Structure

| File | What it is |
|---|---|
| `app/` | The host test app, built as `latency-test.exe`: the test pattern, its logs, the clock sync and upload server |
| `android/` | Latency Cam (see [android/README.md](android/README.md)) |
| `analyzer/analyze_video.py` | The analyzer's entry point: finds the pattern in the video; bundled as `latency-analyzer.exe` |
| `analyzer/camera_mode.py`, `analyzer/camera_timing.py` | Times what the camera saw on the host's clock |
| `analyzer/traces.py` | Joins the results with Sunshine's frame log and Moonlight's pacing trace |
| `analyzer/compare.py` | Compares analysed runs side by side (`latency-analyzer compare`) |
| `analyzer/report.py` | The HTML report |
| `analyzer/build_exe.ps1`, `analyzer/requirements.txt` | Bundle the analyzer into `latency-analyzer.exe` with PyInstaller |
| `analyzer/time_server.py` | Stand-alone version of the app's clock sync server, for testing Latency Cam without the app |
| `tests/` | Unit tests (C++ with GoogleTest, Python with unittest), including an end to end run on a synthetic recording with known latencies |

## What you need

- A Windows PC with `latency-test.exe` and `latency-analyzer.exe` (see [Build](#build) and
  [Deploy to a host](#deploy-to-a-host)). To measure a stream, that's the streaming host.
- The display to measure, where the phone can film it: the PC's own display, or a client streaming from the host. For
  a [baseline](#measuring-a-stream-against-a-baseline), a Windows client also needs `latency-test.exe` itself.
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

To package both exes into the release zip, `LatencyAnalyzer-VERSION-windows.zip` in the build folder (this builds the
analyzer too, with the version from `project()` in `CMakeLists.txt`):

```
ninja -C cmake-build-release release-zip
```

The zip is what goes on a release, next to Latency Cam's APK, and what Sunshine's Latency Test plugin downloads. Its
installer pins the zip's version and SHA-256, so update both there after uploading a new release.

## Deploy to a host

1. Unzip the release zip (see [Quick Start](#quick-start-latency-measurement), or build it with
   `ninja -C cmake-build-release release-zip`) into a folder on the host, such as `C:\Tools\latency-test`. Or, to
   also get the analyzer scripts and this README, install a build into one:

   ```
   ninja -C cmake-build-release latency-analyzer
   cmake --install cmake-build-release --prefix C:\Tools\latency-test
   ```

   Only the two exes are needed on a host.
2. On the host, allow the app through the firewall before the first stream, from an elevated prompt, so Windows
   doesn't ask in the middle of one:

   ```
   netsh advfirewall firewall add rule name="latency-test" dir=in action=allow program="C:\Tools\latency-test\latency-test.exe" enable=yes
   ```
3. In the streaming host, add an app whose command is the full path to `latency-test.exe` (in Sunshine: Applications,
   Add New, Command), or start it on the host's desktop and stream the desktop.

Results go to `results` in that folder, so keep it somewhere the streaming user can write to (not Program Files), or
pass `--results`.

## The test app

`latency-test.exe` options:

| Option | Effect |
|---|---|
| `--vsync` | Starts with v-sync on: waits for vertical blank. By default the app presents straight away, with tearing, so it adds no wait of its own. **V** toggles it while running. |
| `--noise <0..1>` | Background noise amplitude, default 0.5. The noise keeps the encoder near its bitrate; 0 turns it off. |
| `--no-noise` | Flat gray background, so frames stay small (same as `--noise 0`). |
| `--fps <n>` | Background update rate. The default is [RTSS's frame rate limit](#frame-limiters-and-rtss) when RivaTuner Statistics Server is running and sets one for the app, else the display's refresh rate. **F** changes it while running. |
| `--log <path>` | CSV log of each flip, timed with the host's QPC. The default is `%TEMP%\latency_test.csv`. Every frame's render and present times go to the same name ending `.frames.csv`. |
| `--no-auto` | Starts without automatic flips. By default the trigger flips on its own every 150–400 ms, at random, as well as on key presses; automatic flips land on the next scheduled frame, so the frame rate stays steady. **A** toggles them while running. |
| `--auto-interval <min>-<max>` | The interval range for automatic flips in ms, for example `100-300`. |
| `--time-port <n>` | Port answering Latency Cam, default 48100: clock sync over UDP and uploads over TCP. |
| `--no-time-server` | Doesn't answer clock sync requests or accept uploads, and starts on the test pattern, since the phone couldn't reach the app. |
| `--no-qr` | Starts on the test pattern instead of the QR code. |
| `--results <dir>` | Where Latency Cam uploads and their analysis go. The default is a `results` folder next to `latency-test.exe`, or `Latency Cam` in Documents when that can't be written. |
| `--analyzer <path>`, `--python <path>` | The `latency-analyzer.exe` or `analyze_video.py`, and the Python, to run on uploads, if not found next to the app. |
| `--windowed` | Runs in a 1280x720 window instead of fullscreen. |

The app starts on a QR code for Latency Cam. Any key, mouse button or gamepad button brings up the test pattern (Esc
quits), and a press only does that: it doesn't count as an input. Nothing is logged until the pattern shows. The QR
code is generated with [Project Nayuki's library](https://www.nayuki.io/page/qr-code-generator-library) (MIT License),
in `app/third_party/qrcodegen`.

The test pattern shows:

- noise
- the trigger square in the middle, with a row of bits above it (the Gray-coded frame number) and below it (the
  input number)
- a bullseye in each corner
- a readout band at the bottom with the host clock and the time since the last flip
- a status band at the top: the frame rate the app aims for and where it came from, the display's refresh rate,
  v-sync or tearing, the frame rate actually presented over the last second and how long `Present` held the app on
  average, the automatic flips and the Latency Cam port. While a recording is uploading or being analysed, the second
  line shows its progress instead

By default the app presents without v-sync, with tearing. It redraws the background on a timer at `--fps`, or without it at
RTSS's limit or the display's refresh rate. A key press is answered straight away with an extra frame; automatic flips are
shown on the next scheduled frame instead, which keeps the cadence steady for measuring pacing.

Keys, on the host or through the stream; none of them counts as a flip, and each change is noted in both logs
(`# auto=`, `# vsync=`, `# fps=`):

| Key | Does |
|---|---|
| **A** | Toggles automatic flips. |
| **V** | Toggles v-sync. With v-sync on and no `--fps`, the display sets the pace: each present waits for the display to take the previous frame, so frames follow its refresh instead of the app's timer. That adds about a frame of latency. Each frame's line in `.frames.csv` records whether v-sync was on. |
| **F** | Types a new frame rate in the status band: digits, `.` and Backspace edit it, Enter applies it (nothing typed goes back to the default: RTSS's limit or the display's refresh rate) and Esc cancels. A rate a little under the display's maximum, such as 97 at 100 Hz, keeps a VRR display following the app's frames. |
| **Esc** | Quits (or cancels typing a frame rate). While Latency Cam is uploading, an analysis is running or the phone hasn't downloaded the report yet, the test pattern closes and a small window shows the progress; it closes by itself, and the app with it, when everything is done (it gives the phone up to 15 s to start an analysis or download a report). **Close anyway** quits at once: an analysis then carries on in the background and still saves its report on the host, but the phone can't fetch it. |

## Measure

1. **PC:** run `latency-test.exe`, either on the PC whose display you're measuring or streamed from the host. It
   starts on a QR code and answers Latency Cam on port 48100.
2. **Phone:** open Latency Cam on the same network, enter a test name and point it at the display so all four
   bullseyes are in view. When the screen is steady the host switches to the test pattern, the phone counts down
   from 3 and records for 20 s. **Record** also works by hand, once the sync line is green.
3. **Results:** after **Stop**, choose **Upload & analyze**. The host analyses the recording and the phone shows the
   headline numbers, and keeps a copy of the report, which **Open report** shows without the host.

On the host, each recording gets its own folder, `results\NAME\`, holding the video, its timing file, copies of the
host's logs and everything the analysis wrote: `NAME.report.html`, `NAME.summary.json`, `NAME.flips.csv`,
`NAME.frames.csv` and `NAME.regions.png`.

Tips:

- Hold the phone steady or use a stand. Handheld works too, because every frame is re-aligned on the bullseyes.
- All four corner bullseyes must be visible. A client's statistics overlay, such as Moonlight's (Ctrl+Alt+Shift+S),
  can stay on as long as it doesn't cover one.
- Avoid glare on the display, and fill as much of the shot with it as you can: the frame number patches are small.
- Keep `latency-test.exe` running until the upload and analysis are done. It starts its logs afresh each time it
  runs, so a recording uploaded after a restart has nothing to match; the analysis says so.

### Measuring a stream against a baseline

A stream's host present → photon includes the client display's own delay, which can be a large part of it and differs
from one display to the next. To see what the stream itself adds, record a baseline of the client display without the
stream, then stream to it at the same frame rate:

1. **Baseline:** run `latency-test.exe` on the client itself, fullscreen on the display you'll stream to, and record
   it with Latency Cam. This measures the client's own present → photon.
2. **Stream:** stream `latency-test.exe` from the host to the same client and display, and record it again.
3. **Compare:** `latency-analyzer.exe compare results\BASELINE results\STREAM --kinds local stream` (see
   [Compare runs](#compare-runs)). The difference is what streaming adds: capture, encoding, the network, decoding and
   the client's pacing.

Keep everything but the stream the same between the two, above all the frame rate. Latency and pacing both depend on
it: each frame waits up to a frame time at several steps, and VRR only follows frames below the display's maximum. Set
the same rate in both with `--fps` or **F** (the status band shows the rate presented), and keep v-sync, the frame
limiter, the display's refresh rate and its settings the same. The test app on the client and on the host each logs on
its own clock, so the two runs can be filmed one after the other, with Latency Cam pointed at whichever is running.

### Analyse again

To analyse an uploaded recording again, for example after adding [stage logs](#optional-breaking-it-down-by-stage) to
its folder, give the analyzer just its results folder:

```
latency-analyzer.exe results\NAME
```

Or run `latency-analyzer.exe` with no arguments, or double-click it: it lists the results folders it finds (next to
itself, in the current folder and in `Documents\Latency Cam`), newest first, with each one's result and which logs it
holds, and asks which to analyse again. Enter two or more numbers instead, such as `3 1`, to
[compare those runs](#compare-runs), the first as the baseline.

It finds `NAME.mp4`, its timing file, the host log copy and any logs in the folder, takes the stream's size from the
test app's log and its frame rate from what the test app presented while filming, and writes the report back into the
folder.

A recording made elsewhere works the same way: put `NAME.mp4` and `NAME.camera.json` in one folder and pass the host
log from the run (by default the `NAME.host.csv` next to the video, else `%TEMP%\latency_test.csv`, with its
`.frames.csv` next to it):

```
python analyzer/analyze_video.py NAME.mp4 --host-log latency_test.csv --report
```

| Option | Meaning |
|---|---|
| `--stream WxH` | Stream resolution. The default is the test app's, from its log. |
| `--stream-fps N` | Stream frame rate, for frame gridlines. The default is the rate the test app presented at while filming. |
| `--host-log PATH` | latency-test.exe's log from the filmed run. |
| `--camera-timing PATH` | Latency Cam's timing file, if not `NAME.camera.json` next to the video. |
| `--sunshine-frames PATH`, `--moonlight-trace PATH` | [Stage logs](#optional-breaking-it-down-by-stage), if not in the video's folder. |
| `--report` | Also write the HTML report (always, for a results folder). |
| `--out DIR` | Output folder. The default is the video's folder. |
| `--max-frames N` | Analyse only the start of the video. |

### Compare runs

To set two or more analysed runs side by side, for example the test app on the client alone against the same client
streaming it ([a baseline](#measuring-a-stream-against-a-baseline)), or one stream against another after changing a
setting:

```
latency-analyzer.exe compare results\NAME_A results\NAME_B --kinds local stream --labels "Client only" "Streaming"
```

It reads what the analysis already wrote to each folder (nothing is analysed again), prints the comparison and writes
`compare_NAME_A_vs_NAME_B.html` next to the last run's folder (or `--out FILE`). The first run is the baseline the
differences are taken from.

Run with no arguments, it does the same for the runs you pick from the list: it asks what each run without logs was
(local, a stream, or don't know) and a name for each in the report, and offers to open the report when it's done.

`--kinds` says what each run was: `local` (the test app on the client itself, with no stream), `stream`, or `auto`,
the default, which takes a run with Sunshine's or Moonlight's logs to be a stream and leaves the rest unknown. Either
run may have Sunshine's log, Moonlight's trace, both or neither; the comparison uses what each has:

- **Side by side:** host present → photon for flips and every frame, each way, pacing, frames not seen, and the
  difference.
- **Where the time goes:** each run's path from the host present to the photon, step by step, as far as its logs reach.
  With both logs that's capture, first and last packet, decode, the pacer and the client present; with only Sunshine's
  it stops at the capture; a local run is one step, the client's own present → photon.
- **Checks:** that Moonlight's clock sync agrees with Sunshine's send times; that the client display's own delay
  matches between runs that measure it (a local run's present → photon, a stream's client present → photon through
  Moonlight); whether the client's decode and draw leave headroom in the frame time; whether DXGI saw frames queuing
  on the client; whether the frames not seen look dropped or misread; and whether the trigger and the frame patches
  agree on which way the display is slower.
- **Takeaways:** what streaming (or the change between streams) costs, the stream's own part up to the client
  present, pacing, the stages that changed most and the largest parts of each run.

Runs analysed before this version lack two stages it uses when present (capture → last packet, and DXGI's client
present → display): analyse them again to add them.

## Read the results

"Present" alone is ambiguous here, so the analysis never uses it:

- **host present**: the game (the test app) handing its frame to Windows on the host. Without a stream, the "host" is
  the PC running the test app, and host present → photon is that PC's own latency.
- **client present**: the client handing the decoded frame to Windows on the client, before its display has shown
  anything. A client's own latency statistics usually end here.
- **photon**: the client display showing it, which is what the camera sees.

```
host present ─▶ capture ─▶ encode ─▶ network ─▶ decode ─▶ client present ─▶ photon
     │◀─────────────────── camera: host present → photon ─────────────────▶│
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
- **Inside Present:** how long the test app's `Present` call took. It returns at once unless something holds it, and
  the report explains when something did (see below).

### Frame limiters and RTSS

A common way to stream is with v-sync off and a frame limiter such as RivaTuner Statistics Server (RTSS) capping games
at, or a little under, the client's refresh rate. When RTSS is running and sets a limit for `latency-test.exe` (in its
own profile, else the global one), the app aims for that rate unless `--fps` is given, and the status band says "RTSS's
limit". Its own timer then runs 5% faster than the limit, so RTSS alone sets the pace, as it would for a game: with
both aiming for the same rate, either could make a frame wait, and the host's present times would jitter in a way the
client never sees.

The test app logs when it calls `Present` and when `Present` returns, and the analysis looks at the time between:

- **Close to a whole frame, presenting at the display's refresh rate, v-sync off:** frames are queuing on the host. The
  app makes frames a hair faster than the display takes them, so the frames waiting to be shown fill up and every
  new frame waits behind them, adding a frame or two to every latency. A game running uncapped at the refresh rate
  does the same; capping a little below the refresh rate avoids it. In one test at 100 Hz, host present → photon was
  40 ms uncapped and 21 ms capped to 97 fps.
- **Close to a whole frame, below the refresh rate:** a frame limiter is holding the app. RTSS waits inside the app's
  `Present`. Host present times are when the app called `Present`; in RTSS's default mode the frame is handed on
  before the wait, so the wait isn't counted (timing from `Present`'s return instead would read less than the capture
  → photon time, which can't be).
- **With v-sync on,** each `Present` waits for the display, and that wait is part of the latency, as in a game.

### Optional: breaking it down by stage

The camera measures the whole path. To see where the time goes, the analysis can follow every frame it timed through
per-frame logs from the host and the client. Released versions of Sunshine and Moonlight don't write these yet: they
need builds that do, writing:

- **host:** Sunshine's frame log, `sunshine-frames-*.csv`, with each frame's capture, encode and send times on the
  host's QueryPerformanceCounter
- **client:** Moonlight's pacing trace, `Moonlight-pacing-*.csv` (with `ML_PACING_TRACE` set), with each frame's
  packet, decode and present times and, in the `capture_to_present_us` column, Moonlight's own capture to present

Put either or both from the filmed run in the recording's folder (or pass `--sunshine-frames` and `--moonlight-trace`)
and analyse it again. The report and summary get a **Pipeline** section with each stage's time:

| Stage | From |
|---|---|
| host present → capture | the test app and Sunshine, both on the host's QueryPerformanceCounter |
| capture → encoded, capture → sent | Sunshine |
| capture → first packet | Moonlight's capture to present less first packet → client present: the host sending the first of the frame, and the network |
| first packet → last packet → decoder hands it over → GPU decode finished → client present | Moonlight, on the client's clock |
| capture → client present (Moonlight's capture to present) | Moonlight, through its clock sync: the same figure as its statistics |
| client present → photon | the camera's capture → photon less Moonlight's capture to present: the client display's own part |
| capture → photon, host present → photon | the camera (and Sunshine) |

The rows built on Moonlight's capture to present need the `capture_to_present_us` column. With it, the headline
also shows **capture → client present** and **client present
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
| `POST /pattern` | Leaves the QR code for the test pattern; does nothing once the pattern is showing |

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
