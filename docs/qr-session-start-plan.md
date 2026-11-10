# Plan: QR code start for latency-test and Latency Cam

Status: phases 1 to 3 implemented, apart from publishing the signed APK as a release. A real gamepad and a full stream
are still to try. Saved 2026-11-09.

## Goal

Make starting a test hands-off. `latency-test` opens on a QR screen. Scanning it with the phone's camera app opens
Latency Cam, or the GitHub releases page if it isn't installed. When Latency Cam sees the QR itself, it switches the
host to the pattern, counts down 3, 2, 1 and starts recording. Any key or gamepad button on the host also goes to the
pattern.

Rough size: 3–4 days.

## Decisions

- Repo: <https://github.com/MoreOrLessSoftware/latency-analyzer>. Releases:
  <https://github.com/MoreOrLessSoftware/latency-analyzer/releases>.
- Link and redirect page hosted on GitHub Pages (see phase 3 for the catch with `assetlinks.json`).
- The QR screen is the default; `--no-qr` goes straight to the pattern.
- Recording stops by itself after about 20 s, and Stop still works earlier.

## Flow

1. `latency-test` starts on the QR screen, with the usual four bullseyes in the corners.
2. The user scans it with the camera app. Latency Cam opens if installed; otherwise the browser goes to the releases
   page.
3. Latency Cam decodes the same QR from its own preview and takes the host address from it.
4. Once the whole display is in frame and clock sync has samples, the app tells the host to switch to the pattern.
5. The phone counts down 3, 2, 1 and starts recording. Tapping cancels the countdown.

Any key, mouse button or gamepad button on the host skips to the pattern, and the app's manual Record button keeps
working. Esc still quits.

## Test first

Results on a Pixel 10 Pro, 2026-11-09, using a throwaway test build:

- **ZXing from the 240 fps preview: works, straight off a monitor.** 85 of 86 frames decoded (the one miss was the first
  frame), about 8 ms per decode, with a 37-module QR at only about 28% of the display's height. ZXing 3.5.3,
  `HybridBinarizer`, no `TRY_HARDER`.
- **`#…` part of the URL with the Pixel Camera: kept in full.** The app received the whole URL through `onNewIntent`.
  The link was marked approved with `pm set-app-links-user-selection`, so real domain verification is still untested.
- **Still to test:** reading the QR through the stream's compression (Moonlight), and the Samsung camera.

## Phase 1: latency-test (about 1 day)

- **QR content:** one URL, `https://moreorlesssoftware.github.io/c#h=192.168.1.20&p=48100&s=3840x2160`. `h` is a comma
  separated list of addresses, `p` the port and `s` the display size. The fragment keeps LAN addresses out of web
  server logs. Generate it with Nayuki's single-file MIT QR library, vendored under `app/` so the build needs no
  download.
- **Host address:** `GetAdaptersAddresses` (link `iphlpapi`) lists active non-loopback IPv4 addresses, up to three,
  because VPN and Hyper-V adapters are common. The app picks the one on its own Wi-Fi subnet, else the first.
- **Drawing:** a separate `render_qr()` centres the QR at a fixed fraction of the display height, snapped to whole
  pixels with a white quiet zone, at about 30 fps so it survives capture and new client connections. Draw the
  bullseyes from the same `layout_t` as the pattern, as a framing target. The main loop branches to it before the
  frame pacing.
- **Logs:** QR frames aren't logged. The frame count and automatic flips start when the pattern starts
  (`schedule_auto_flip`, reset `next_frame`), so the analyzer doesn't change.
- **Switching from the phone:** add `POST /pattern` to `route_request` in `app/http.h`, before the `/recordings/`
  prefix check, with tests in `tests/test_http.cpp`. It calls a callback in `analysis_config_t` (like `flush_logs`)
  that sets a `std::atomic<bool>`, because the server runs on its own thread. Safe to call twice.
- **Gamepad:** input is only keyboard and mouse today. Add XInput polling through `LoadLibrary("xinput1_4.dll")`, which
  keeps the exe standalone, plus raw HID buttons (usage page 1, usage 5) for DualShock-style pads. Only used on the QR
  screen.
- **Key handling on the QR screen:** a press only leaves the QR screen; it doesn't count as an input or apply A, F, V
  or N. Esc still quits.
- **Launch grace period:** ignore input for about the first second, or the Enter or A press that launched the app from
  Moonlight could skip the QR through key repeat.
- **When it's skipped:** `--no-qr`, and also `--no-time-server`, since the phone couldn't talk to the host.
- Update the README's options and Measure sections.

## Phase 2: Latency Cam (1–2 days)

- **Reading the QR:** the camera runs a `SESSION_HIGH_SPEED` session, which only allows the preview and encoder
  outputs, so an `ImageReader` can't be added. Grab `preview.getBitmap(720, 1280)` on the UI thread a few times a second
  and decode on a worker thread with ZXing core, `QR_CODE` only. It must be the portrait size: `getBitmap` ignores the
  view transform from `fitPreview()`, so a landscape 1280x720 grab is stretched and ZXing read 0 of 389 such frames. The
  portrait grab has the right proportions but is rotated 90°. ZXing is small and doesn't need Google Play services.
  Scanning runs only on the Recording tab, with the camera open, while not recording.
- **Framing check:** the analyzer rejects runs missing a bullseye. The QR's size and position on the display are fixed,
  so ZXing's finder points plus `s=` predict where the display's corners are: repeat `place_qr` from `app/pattern.h`
  with the code's module count to get its pixels per module. The finder points also give the QR's
  orientation, so the 90° rotation needs no special handling. Show "move back" or "centre the screen" until the corners
  are inside the frame and steady for about a second.
- **Countdown:** a state machine in `MainActivity`: scanning, aligning, `POST /pattern`, 3-2-1, then the existing
  `toggleRecording()`. It needs the recorder ready and clock sync with samples and a round trip under 5 ms; it doesn't
  wait for the 10 s offset-wander figure. Tapping, leaving the tab or `onPause` cancels it.
- **Automatic stop:** stop about 20 s after the recording starts, for every recording, showing the time left on the
  Stop button.
- **Manual Record:** also sends `POST /pattern`, so nobody can film the QR screen by accident.
- **Host from the QR:** `clockSync.setHost()` with the chosen address, and save it like the Host field does. Only
  accept private IPv4 addresses.

## Phase 3: link and release (about half a day, plus setup)

A custom URL scheme can't fall back to a download, and `intent://` links are unreliable from scanners. A verified
Android App Link is the only way to get "opens if installed, else redirect" from one scan.

- **Where `assetlinks.json` goes:** Android only looks at `/.well-known/assetlinks.json` at the root of the domain. A
  project site (`moreorlesssoftware.github.io/latency-analyzer/`) can't serve that path, so this needs a separate user
  site repo, `MoreOrLessSoftware/MoreOrLessSoftware.github.io`, or a custom domain on the project site. The user site
  needs a `.nojekyll` file so the `.well-known` folder is published.
- **Redirect page:** `/c/index.html`, a static redirect to the releases page.
- **Signing:** create a release key and add a signing config to `android/app/build.gradle`. Its SHA-256 fingerprint
  goes in `assetlinks.json`. Keep the key out of the repo.
- **Android:** an `https` intent filter with `android:autoVerify="true"` for `moreorlesssoftware.github.io` and path
  `/c`, and `singleTask` with `onNewIntent` on `MainActivity`.
- **Link handling:** the link only pre-fills the host and never starts anything, so a malicious link can't trigger a
  recording; the countdown needs the QR in the camera's view.
- **If verification fails** on an installed copy, the user lands on the releases page, but opening the app by hand
  still works because it reads the QR itself.
- **Release:** publish a signed APK on the releases page.

## Order of work

1. Feasibility tests above.
2. Phase 1, tested with the existing app's manual flow and `curl -X POST`.
3. Phase 2.
4. Phase 3.
