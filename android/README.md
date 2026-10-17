# ECG Recorder (Android)

Long-recording app for a single-lead ECG streamed from a USB (CH340) device over an OTG cable.
It records inside a foreground service (keeps working with the screen off), writes a CSV that the
ECG service (`web/`) analyses directly, and uploads the recording when it stops.

The in-app UI labels are in Serbian (`DOBAR` / `SLAB` / `OTPALA ELEKTRODA` / `SATURACIJA` = good /
weak / electrode off / saturation); the CSV field names and the wire format are language-neutral.

## Why it exists

- **Web Serial in the browser** only works while the page is open and the screen is on; for a
  10–30 minute recording while lying down that breaks (the page goes to the background and the
  connection is dropped).
- **A WebView (in-app browser)** supports neither Web Serial nor WebUSB — it will never work.
- The app uses the **Android USB Host API** (`usb-serial-for-android`), so it does not depend on a
  browser at all.

## Features

- **Live waveform** (`LiveView`): the last ~5 s with automatic scaling, peak-to-peak amplitude and
  a contact state (good / weak / electrode off / saturation) — use it before recording to check
  electrode placement.
- **Connect and record are separate**: `CONNECT` opens the port and only displays the waveform;
  recording starts on `REC_START` and `REC_STOP` closes the file (the connection stays open for the
  next recording).
- **Automatic pause when the electrode loses contact**: a flat signal (< 20 ADC units peak-to-peak)
  or saturation (> 50 % of samples on the rail) for more than 3 s stops the write and puts
  `# PAUSE t=MM:SS label=elektroda otpala (ravna linija)` in the CSV; after 1 s of good signal a
  `# RESUME` follows. Manual pause is a separate button, and the auto-pause can be switched off.
- 9600 baud, 8N1 — no parameters to choose, the module is wired for that rate.
- A **timestamp per sample**, evenly spaced at 1/192 s inside a read chunk; nominal rate 192 Hz
  (9600 baud / 5 bytes per line).
- **Phase markers**: buttons write `# MARK t=MM:SS label=...` into the CSV — the service ignores them
  (they are `#` comments) while `tools/ecg_phases.py` reads them and attaches the current heart rate
  to each marker.
- Auto-reconnect up to 20 × 2 s when the CH340 re-enumerates on USB.
- Live readout: elapsed time, sample count, measured sample rate, heart-rate estimate.
- **Upload to the service** → multipart POST to `api.php?action=submit` (`data_file`, `fs=192`,
  `source=android-app`), then polling `?action=status`.
- The file stays on the phone: `Android/data/org.ecgrecorder/files/ecg/ECG_YYYYmmdd_HHMMSS.csv`.

## Recording format

```
Timestamp,S1
# MARK t=00:00.0 label=mirovanje
2026-10-16T11:00:00.000+02:00,505
2026-10-16T11:00:00.005+02:00,507
...
# STOP t=06:00 samples=69120 measured_fs=192.00 bad_lines=0
```

(`label=mirovanje` = "resting"; labels are free text written by the user.)

## Build (without Android Studio)

```bash
apt-get install -y unzip openjdk-21-jdk
# Android SDK (cmdline-tools) in /opt/android-sdk; platform 34 + build-tools 34.0.0
gradle -p . assembleDebug          # from the android/ directory
# APK: app/build/outputs/apk/debug/app-debug.apk
```

`local.properties` holds `sdk.dir=/opt/android-sdk` (not committed — Gradle/Android Studio
generates it). The project ships without a Gradle wrapper: open it in Android Studio, or run
`gradle wrapper` once and then use `./gradlew`.

Set the server address in one place:
`app/src/main/java/org/ecgrecorder/Uploader.java` → `BASE` (and the dashboard link in
`MainActivity.java`).

## Verification (no phone needed)

1. `gradle assembleDebug` succeeds → the APK exists.
2. `aapt2 dump badging app-debug.apk` → package `org.ecgrecorder`, minSdk 24, targetSdk 34,
   permissions (INTERNET, FOREGROUND_SERVICE, FOREGROUND_SERVICE_CONNECTED_DEVICE, WAKE_LOCK).
3. **The recording format is tested against the service**: a CSV in exactly the app's format
   (ISO timestamps with milliseconds and an offset, `# MARK`, `# STOP`) is pushed through
   `web/ecg_analyze.py --outdir /tmp/...` (which does not touch `data/`) → 69,120 samples,
   measured rate 192.0 Hz, "ISO timestamp column (69120 distinct times)"; the markers do not
   disturb the parse.
4. The same recording through `tools/ecg_phases.py` → the 60/100/75 bpm phases are recognised, and
   each marker gets the heart rate at that moment (e.g. `2:00.0 → 100.2 bpm`).
5. **Auto-pause test**: a recording in the app's format with a 30 s pause (no samples between
   150 s and 180 s, with `# PAUSE ... label=elektroda otpala (ravna linija)` and `# RESUME`) →
   `ecg_phases.py` reports a 60 bpm phase, then
   `PAUZE: 2:30.0-3:00.0 (30 s) elektroda otpala (ravna linija)`, then a 90 bpm phase; segments that
   fall inside the pause are not interpreted.

⚠️ **A recording whose state changes must not be interpreted from the service's single report**:
autocorrelation and the periodicity check fail (in the test: "count 75 vs autocorrelation 100,
25 % difference → HR unreliable"), so the report correctly refuses to interpret it. For such
recordings the authoritative tool is `tools/ecg_phases.py` (per-phase metrics).

`tools/test_recorder_logic.sh` runs the auto-pause state machine against synthetic signals on a
desktop JVM (no device, no Android SDK): it compiles the real `Recorder.java` against a stub
`android.util.Log` and checks pause/resume pairing, that samples are still written inside pauses,
and that the marker lines carry their numbers.

## Limitations

- One channel, AD8232, no calibration in mV → morphology and ST are not interpreted clinically.
- Android 14+ requires `foregroundServiceType="connectedDevice"` (declared in the manifest).
- The USB permission is requested on every new connection (unless it was remembered).
- The app is not on Play Store — it is installed as an APK (debug signature).
