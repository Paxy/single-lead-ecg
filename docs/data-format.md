# Data format and API

## 1. Recording CSV (what the recorder produces)

```
Timestamp,S1
2026-01-01T10:00:00.000,512
2026-01-01T10:00:00.005,517
# PAUSE t=00:17 iso=2026-01-01T10:00:17.000 pp=950 jump=51.0% hf=0.94 sat=5% state=SUM auto=1 label=šum/pokret (loš talas)
...
# RESUME t=00:32 iso=2026-01-01T10:00:32.000 pause_s=15 pp=520 jump=0.0% hf=0.30 sat=0% state=OK label=snimanje
# Q t=00:35 pp=330 jump=0.2% hf=0.30 sat=0% state=OK
2026-01-01T10:00:35.000,498
# MARK t=03:12 label=ležanje
# STOP t=05:00 iso=... samples=57600 skipped_while_paused=0 paused_s=15 pauses=1 gaps=0 gap_s=0 measured_fs=191.87 bad_lines=0 label=kraj
```

Rules:

- **One column pair**: `Timestamp` (ISO 8601, local time with offset or `Z`) and `S1`
  (raw ADC 0…1023). Older exports used a numeric `S1` column only; the loader accepts a
  single numeric column too.
- **Every `#` line is a marker, never a sample.** The main engine reads the CSV with
  `comment="#"`; the phase/report tools parse the markers explicitly. Marker kinds:
  `# START`, `# STOP`, `# PAUSE`, `# RESUME`, `# GAP`, `# MARK`, `# Q`.
- **`# PAUSE` is not a cardiac pause.** It means the recorder stopped writing because the
  signal was noise, saturated, or the electrode fell off. Two recording generations exist:
  - older export: samples inside the pause are *missing* from the file (a hole in time);
  - app 2.1+: samples inside the pause are *present* (raw), and the pause interval is
    excluded by the analysis via the marker + `keep_mask`.
  Recover the active duration as `duration − dropped_s`; never compute heart rate across it.
- **`# GAP`** marks a data-flow interruption (phone/USB stalled) — also not a cardiac pause.
- **`# MARK`** is a free label written by the user (posture, symptoms, start of exercise).
  `ecg_phases.py` attaches the current heart rate to each marker.
- **`# Q`** lines carry the quality numbers the recorder used to decide to pause
  (`pp`, `jump`, `hf`, `sat`, `state`). If a pause looks unjustified, these numbers are the
  audit trail.
- Timestamps have **millisecond resolution** and may repeat for a whole serial read chunk;
  the analyser interpolates inside a chunk to keep the sample rate finite.

## 2. File layout on the server

```
web/
├─ index.php            dashboard: last analysis, KPI cards, history table
├─ plotter.html         acquisition UI (Web Serial + simulator + CSV export)
├─ api.php              JSON API (submit / status / latest / history / session)
├─ ecg_analyze.py       analysis engine (see docs/analysis.md)
├─ ecg_medical.py       median beat, interval measurement, medical wording
├─ ecg_segments.py      consensus across beat partitions and time segments
├─ report.sh            CLI wrapper around ecg_analyze.py
└─ data/                (runtime, git-ignored)
   ├─ data.csv          current recording
   ├─ data.json         full result (everything the UI shows)
   ├─ data.txt          human-readable report
   ├─ data_report.png   ECG + trend figure
   ├─ data_intervals.png intervals / median beat figure
   ├─ job.json          background job state
   └─ history/<id>/     last N sessions (csv + json + txt + png)
```

## 3. HTTP API

All endpoints are `GET` unless stated; responses are JSON. Errors are
`{"ok": false, "error": "..."}` with an HTTP 4xx/5xx status.

| Endpoint | Method | Body / params | Returns |
|---|---|---|---|
| `api.php?action=submit` | POST multipart | `data_file` (CSV), `fs` (nominal Hz), `source` (`csv`/`android-app`/…) | `{"ok":true,"job_id":"YYYYmmdd-HHMMSS"}` — analysis runs as a **background job** |
| `api.php?action=status` | GET | — | `{"ok":true,"state":"running|done|error","error":…}` |
| `api.php?action=latest` | GET | — | last job + full `results` object |
| `api.php?action=history` | GET | — | list of sessions: id, when, HR, beats, duration, quality, PR/QRS/QTc |
| `api.php?action=session&id=<id>` | GET | `id=YYYYmmdd-HHMMSS` | `results` of that archived session |

Notes:

- The analysis is **never** done inside the HTTP request: uploads can be long, and a proxy
  in front of the service may cut a request at ~100 s. `submit` writes `job.json`
  (`state=running`), spawns `setsid nohup python3 ecg_analyze.py …&` and returns at once;
  the client polls `status`.
- A `flock` on `data/.analysis.lock` allows one analysis at a time.
- If the service sits behind Cloudflare, **the client must send a real `User-Agent`**
  (the default `Python-urllib/3.x` is refused with `403 error code: 1010`).
- `submit` **overwrites** `data/` and appends a session to `data/history/`. Test uploads
  therefore need a restore step; the CLI (`report.sh --outdir /tmp/x`) does not touch `data/`.

## 4. Result JSON — the parts that matter

`data.json` is a flat object; the UI reads it directly. Keys:

| Key | Content |
|---|---|
| `sampling` | nominal/measured/active fs, samples, duration, `dropped_s`, `n_pauses`, `n_gaps` |
| `signal` | min/max/mean/std of the raw signal |
| `heart` | HR statistics (`median`, `mean`, `iqr`, `p5/p95`), `beats`, `rr_artifacts`, `pauses_over_2s`, and the three-method cross-check (`hr_detection`, `hr_autocorr`, `independent_hr`, `cross_check_ok`, `periodicity_corr`) |
| `hrv.time` | `sdnn_ms`, `rmssd_ms`, `pnn50_pct`, `sd1/sd2`, `cv_pct` |
| `hrv.freq` | `lf_ms2`, `hf_ms2`, `lf_hf`, `lf_nu/hf_nu`, `duration_s`, `vlf_ms2` (**null** for records < 10 min) |
| `intervals` | beat-by-beat (dwt) statistics for `pr/qrs/qt/qtc_bazett/qtc_fridericia` + `qrs_iqr_spread_ms` |
| `median_beat` | QRS onset/offset, P wave (`peak_ms`, `on_ms`, `off_ms`, `amp`, `snr`, `width_ms`, `pr_ms`, `present`), T wave (`end_ms`, `qt_ms`, `reliable`), `template_corr`, `n_beats` |
| `consensus` | `pr/qrs/qt` consensus objects (`median`, `iqr_ms`, `min_ms`, `max_ms`, `n_ok`, `n_total`, `ok`, `reason`, `source`), `rows` (partitions), `segment_rows` (30 s segments), `state_change`, `hr_range_pct` |
| `quality` | internal 0–100 score + components (`clipped_pct`, `hf_noise_pct`, `powerline_pct`, `baseline_wander_pct`, `rr_artifact_pct`, `delineation_pct`) |
| `interpretations` | list of technical sentences (the text report) |
| `layman` | the same findings in plain language, no abbreviations |
| `medical` | structured medical section: `lines`, `checks`, `anomalies`, `cannot`, `verdict`, `next_steps`, `pr_used_ms`, `qtc_ms` |
| `extra` | ectopics/pauses, respiration, nonlinear HRV (DFA α1, SampEn), noise timeline, relative ST trend |

`session` holds the archive id; `files` names the produced artifacts.

## 5. Marker-tolerant reading (important for contributors)

Anything that parses a recording must do three things, or it will report false findings:

1. **strip `#` lines** before building the time axis;
2. **exclude dropped intervals** from RR/interval statistics (a hole in the time axis is not
   a cardiac pause — a 72 s RR is a stall, not a diagnosis);
3. compute the sample rate on the **active** part (`active samples / active seconds`), not
   over the whole axis, otherwise every recording with pauses looks like it lost samples.
