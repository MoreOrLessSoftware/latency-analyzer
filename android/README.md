# Latency Cam

An Android app that films the client display at 240 fps while keeping the phone's clock synced with the host's
QueryPerformanceCounter. Every camera frame then has a time on the host's clock, so the analyzer can time what the
camera saw against what the host logged.

## Build and install

Needs the Android SDK (the path goes in `local.properties`, as `sdk.dir=...`) and a JDK 17 or newer, such as the one
bundled with Android Studio:

```
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The phone needs a back camera with constrained high speed video at 1920x1080 and a fixed 240 fps, and camera
timestamps from the `REALTIME` source. Check with `adb shell dumpsys media.camera`
(`availableHighSpeedVideoConfigurations` and `android.sensor.info.timestampSource`). The Pixel 10 Pro qualifies.

## Use

1. On the host, run `latency-test.exe`, which answers clock sync requests on UDP port 48100 and takes uploads on TCP
   port 48100. Allow it through the firewall when asked. Without the app, `python analyzer/time_server.py` answers them instead.
2. Open Latency Cam on the phone, on the same network as the host. It finds the time server by broadcast, or type
   the host's IP. The sync line turns green once the fastest round trip is under 5 ms and the offset stays within
   0.75 ms over 10 s.
3. On the **Recording** tab, enter a test name and point the phone at the QR code `latency-test` starts on. Once the
   whole screen is steady in view the app brings up the test pattern, counts down from 3 (tap to cancel) and records
   for 20 s. Or press **Record** at any time: the button turns red and shows the time left; press it again to stop
   early. Each recording is saved to `Download/LatencyCam` as `NAME_DATE-TIME.mp4` with
   `NAME_DATE-TIME.camera.json` next to it.
4. Choose **Upload & analyze** to send both files to `latency-test.exe` on port 48100, which runs the analyzer. The
   app switches to the **Results** tab and shows the headline numbers when it's done. It also downloads the report to
   `Download/LatencyCam/NAME.report.html`, next to the recording, and **Open report** shows that copy in the app, so
   it can be read after `latency-test.exe` has closed. **Retry upload** appears if it fails. **Discard** deletes the
   recording from the phone.
   Recording again without choosing keeps the last one on the phone, to copy over later, for example with
   `adb pull /sdcard/Download/LatencyCam/`.

Recordings are 60 Mbps, about 225 MB for 30 s.

The clock sync keeps running for 2 s after **Stop** before the files are written, so the drift fit covers the
whole recording.

## The timing file

`NAME.camera.json` holds:

| Key | Contents |
|---|---|
| `video_pts_us` | Presentation time of every video frame, µs of `CLOCK_MONOTONIC`. Frame N of the video in display order has the Nth smallest. |
| `captures` | One row per frame: sensor timestamp (start of exposure of the first row, ns of `elapsedRealtimeNanos`), exposure, rolling shutter skew and frame duration, in ns. |
| `clocks_at_start`, `clocks_at_stop` | `elapsedRealtimeNanos` and `System.nanoTime` read together, to move `video_pts_us` onto the capture clock. |
| `sync.samples` | Every clock sync exchange: phone ns when sent (t1), host QPC ticks on arrival (t2) and reply (t3), phone ns when the reply came back (t4). `sync.qpc_frequency` converts the ticks. |
| `sensor_orientation`, `width`, `height`, `fps`, `device`, `camera_id` | Recording details. The video is stored in the sensor's own orientation. |

The host's QPC time of a pixel at row `r` of frame N, ignoring the sync fit, is about:

```
capture timestamp + r / height × rolling shutter skew + exposure / 2 + (host QPC ns − phone ns offset)
```

where the offset comes from fitting `((t2 − t1) + (t3 − t4)) / 2` against time, using the exchanges with the fastest
round trips.

## Clock sync protocol

UDP port 48100, little endian.

- Request, 16 bytes: magic `MLCS` (`0x53434C4D`), sequence number (u32), t1 (i64, phone ns).
- Reply, 40 bytes: magic `MLCR` (`0x52434C4D`), sequence number, t1 echoed, t2 and t3 (i64, host QPC ticks), QPC
  frequency (i64, ticks per second).
