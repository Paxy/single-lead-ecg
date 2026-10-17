# Single-lead ECG — acquisition, Android recorder and analysis

A small, end-to-end, **non-medical** single-lead ECG project:

- **hardware**: AD8232 front end on an Arduino-class board, 3 electrodes, 9600 baud ASCII;
- **acquisition**: browser (Web Serial) plotter, and an Android app for long recordings;
- **analysis**: Python engine (neurokit2) with quality control, interval measurement,
  segment consensus, HRV and a plain-language report;
- **service**: PHP dashboard + JSON API, background analysis jobs, session archive.

> ⚠️ **Not a medical device, not a diagnosis.** Single lead, uncalibrated amplitudes,
> 192 Hz sampling. It cannot see electrical axis, hypertrophy, infarction/scar, mV-level ST
> changes, bundle-branch block type, WPW or the arrhythmia type. Never use it to make a
> treatment decision, and never on another person. See [docs/analysis.md](docs/analysis.md) §8
> for the mapping to published standards (and what is deliberately not claimed).

## Repository layout

```
web/        PHP service + Python analysis engine (deploy this on a server)
android/    Android recorder app (Java, USB-serial over OTG)
firmware/   reference firmware for the AD8232 acquisition board
tools/      CLI helpers: phase splitting, cross-checks, synthetic recordings, analysis over many sessions
docs/       hardware, data format/API, analysis pipeline, deployment
```

## How it fits together

```
 electrodes ─► AD8232 ─► MCU ──USB serial 9600 8N1, "value\r\n"──► browser / Android app
                                                                    │  CSV (timestamp,value,+markers)
                                                                    ▼
                                                      PHP API (background job) ─► Python engine
                                                                    │
                                                          data.json / data.txt / 2 PNG
                                                                    ▼
                                                       dashboard, history, session view
```

Two acquisition paths, one CSV format:

- **`web/plotter.html`** — Web Serial in Chrome, live waveform, filter for display only,
  CSV export, one-click analysis. Fine for 30–120 s spot checks.
- **`android/`** — foreground service, keeps recording with the screen off and the app
  backgrounded, writes `# PAUSE`/`# RESUME` markers when the electrode loses contact,
  uploads the recording when it stops. Needed for 5–30 min recordings.

## Quick start

```bash
# 1) server (Debian/Ubuntu, Apache + php-fpm) — see docs/deployment.md
python3 -m venv /opt/ecg-venv && /opt/ecg-venv/bin/pip install numpy scipy pandas matplotlib neurokit2
mkdir -p /var/www/html/ecg/data && cp web/* /var/www/html/ecg/ && chown -R www-data:www-data /var/www/html/ecg/data

# 2) analyse a recording without any hardware
python3 tools/gen_test_ecg.py regular 72 60 > /tmp/test.csv
/opt/ecg-venv/bin/python3 web/ecg_analyze.py --input /tmp/test.csv --outdir /tmp/out --fs 192
cat /tmp/out/data.txt

# 3) firmware: open firmware/ecg_ad8232/ecg_ad8232.ino in the Arduino IDE, flash, wire per docs/hardware.md
# 4) Android: open android/ in Android Studio, or `gradle wrapper` then `./gradlew assembleDebug`
#    and set the server URL in android/app/src/main/java/org/ecgrecorder/Uploader.java
```

## What the report contains

1. **Plain-language section** (no abbreviations): what was measured, what it usually means,
   what this recording cannot show, what to change if the signal was poor.
2. **Heart rate and rhythm** with the three-method cross-check.
3. **Intervals** from the median beat — PR with a consensus gate, QRS/QT reported as
   measurements (see below).
4. **HRV** (time and frequency domain, Task Force rules) + respiration from baseline wander.
5. **Quality**: dropped time, clipping, mains hum, baseline wander, RR artefacts.
6. **Medical section**: normal ranges, reliability conditions per parameter, and the explicit
   list of what a single lead cannot establish.
7. **Consensus table**: the same measurement repeated over 4 beat partitions and 30 s
   segments — if the groups disagree, the value is not interpreted and the spread is printed.

## Two honest limitations worth stating up front

- **QRS looks narrow.** On this hardware the engine reports ~55–65 ms where a 12-lead ECG
  measures 74–114 ms. That is the measurement method (single lead, 192 Hz, derivative-based
  onset), not a finding — so QRS is reported as a measurement and never classified as
  "narrow/wide".
- **QT/QTc is not interpreted.** The tangent method on the median beat and the beat-to-beat
  method differ by >100 ms here (published method differences are ~10 ms), so no
  "normal/prolonged" verdict is given and no QTc threshold is applied.

Both are consequences of the hardware class; a 24-bit ≥500 Hz front end (ADS1292R/ADS1298)
plus calibration and validation against annotated databases is the way out (see
[docs/hardware.md](docs/hardware.md) §8).

## Documentation

| File | Content |
|---|---|
| [docs/hardware.md](docs/hardware.md) | BOM, wiring, electrode placement, gain/saturation, noise, calibration, upgrade path |
| [docs/data-format.md](docs/data-format.md) | CSV format and markers, file layout, HTTP API, result JSON |
| [docs/analysis.md](docs/analysis.md) | pipeline, artifact handling, median beat, consensus, HRV, standards mapping, what is not interpreted |
| [docs/deployment.md](docs/deployment.md) | server setup, PHP limits, background jobs, updating and re-analysing |
| [android/README.md](android/README.md) | app behaviour, CSV contract, build |
| [firmware/README.md](firmware/README.md) | firmware behaviour and wire format |
| [tools/README.md](tools/README.md) | CLI helpers |

## Privacy

The repository ships **no recordings, no history and no credentials**. `data/` is created at
runtime and is git-ignored. Recordings are personal health data: keep them out of version
control, out of issue trackers and out of screenshots.

## License

MIT — see [LICENSE](LICENSE). No warranty; not a medical device.
