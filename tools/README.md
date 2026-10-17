# CLI tools

These operate on recordings in the CSV format described in `docs/data-format.md`.
All of them expect the service's Python environment (`/opt/ecg-venv/bin/python3`, i.e.
numpy/scipy/neurokit2) — set `ECG_PYTHON` or call that interpreter explicitly.

| Tool | Purpose |
|---|---|
| `gen_test_ecg.py` | synthetic recordings with known morphology: `regular <bpm> <sec>`, `brady`, `af`, `multi 60:120,100:120,75:120`, `app <bpm> <sec>` (app 2.1 format with pause markers and samples inside pauses) |
| `ecg_phases.py` | splits a long recording into phases/segments (`--seg 30`), reports HR, RR irregularity, ectopics and pauses per phase, reads `# PAUSE`/`# MARK`; use it whenever the state changed during the recording |
| `long_recording_analysis.py` | sliding 30 s windows (peak median HR, windows ≥100 bpm, beats with RR < 600 ms) + 5-minute HRV windows, compared against a baseline recording |
| `ecg_crosscheck.py` | independent verification of heart rate: three detectors (neurokit peaks, 5–25 Hz envelope, autocorrelation), on active parts only |
| `morphology_sessions.py` | same engine over many sessions → table + shape-similarity matrix + ASCII median beats (only comparable with identical electrode placement) |
| `morph_fig.py` | renders the median-beat comparison as a figure |
| `test_recorder_logic.sh` | compiles the Android `Recorder.java` against stubs and runs synthetic signals through the pause/resume logic (no phone needed) |

Typical loops:

```bash
# is the engine still correct after a change?
for f in /tmp/reg.csv /tmp/brady.csv /tmp/af.csv; do
  /opt/ecg-venv/bin/python3 ../web/ecg_analyze.py --input $f --outdir /tmp/o --prefix $(basename $f .csv) --fs 192
done

# did the state change during a 10-minute recording?
/opt/ecg-venv/bin/python3 ecg_phases.py /tmp/long.csv --seg 30 --json /tmp/phases.json
```
