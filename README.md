# ml-latency-analyzer

Measures a stream's latency from a key press with a phone camera. The test needs no clock sync and no footage of a
finger pressing a key, because both events it times appear on the same screen:

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
| `analyzer/time_server.py` | Host clock server for Latency Cam |
| `android/` | Latency Cam, an Android app that records 240 fps video with every frame timed on the host's clock (see [android/README.md](android/README.md)) |

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

## Set up

1. In Sunshine's Applications page, add an app whose command is the full path to `latency-test.exe`. Its options:

   | Option | Effect |
   |---|---|
   | `--vsync` | Waits for vertical blank. By default the app presents straight away, with tearing, so it adds no wait of its own. |
   | `--noise <0..1>` | Background noise amplitude, default 0.5. The noise keeps the encoder near its bitrate; 0 turns it off. |
   | `--no-noise` | Flat gray background, so frames stay small (same as `--noise 0`). |
   | `--fps <n>` | Background update rate. The default is the display's refresh rate. |
   | `--log <path>` | CSV log of each input and the frame that answered it, timed with the host's QPC. The default is `%TEMP%\sunshine_latency_test.csv`. |
   | `--windowed` | Runs in a 1280x720 window instead of fullscreen. |

2. Stream the app. The screen shows:
   - noise
   - the trigger square in the middle, with a row of bits above it (frame number) and below it (input number)
   - a bullseye in each corner
   - a readout band at the bottom with the host clock and the time since the last input
3. Press **Ctrl+Alt+Shift+T** in Moonlight to turn the marker on. It appears a quarter of the way across the screen,
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
