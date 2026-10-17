# Analysis pipeline

The engine is `web/ecg_analyze.py` (+ `ecg_medical.py`, `ecg_segments.py`). It runs from the
CLI (`report.sh`) or as a background job triggered by `api.php?action=submit`.

```
CSV ─► load & clean ─► keep_mask (exclude # PAUSE intervals)
     ─► 0.5–40 Hz clean ─► R peaks (neurokit) ─► artifact correction (double counts)
     ─► HR (3 methods, cross-check) ─► RR / HRV (time + frequency)
     ─► 1000 Hz? no: 0.5–75 Hz "wide" signal ─► median beat ─► P/QRS/T + intervals
     ─► consensus (4 beat partitions + 30 s segments)
     ─► quality score ─► interpretations + layman text + medical section ─► JSON/TXT/PNG
```

## 1. Loading and the "dropped interval" problem

The loader accepts `Timestamp,S1` (ISO 8601) or a single numeric column. `#` lines are
comments. `# PAUSE`/`# RESUME`/`# GAP` are parsed into **dropped intervals**:

- peaks detected inside a dropped interval are discarded;
- any RR pair that touches one is excluded from statistics;
- the sample rate is computed on the **active** part only;
- quality score is penalised for the dropped fraction (a recording with 22 % dropped time is
  "good", not "excellent").

Why this matters: without it, a 30-minute recording with app pauses produced over a hundred fake
"pauses in rhythm" and a ~36 s RR — a cardiological finding invented by a data hole.

## 2. Beat detection and the double-count trap

`neurokit2.ecg_peaks` on a 0.5–40 Hz cleaned signal. Two known failure modes are handled:

1. **T wave counted as R** → `correct_double_counts()`: if an RR is shorter than
   0.62 × the expected RR, keep only the larger peak. The expected RR comes from
   autocorrelation — **only when that autocorrelation is credible** (`ok` and
   `peak_corr ≥ 0.30`).
2. **Every second real beat removed** (the opposite error): if the removed peaks have a
   similar amplitude to the kept ones (`removed/kept > 0.60`) or more than 45 % of peaks were
   removed, the correction is reverted. A recording of ~80 bpm once came out as
   **40 bpm** because the autocorrelation locked onto 2× the period and the correction then
   deleted every other real R wave.

Post-correction validation goes to an **independent** counter (5–25 Hz envelope + peak
finder). Comparing against the same autocorrelation is circular and only confirms the error.

## 3. Heart rate — three numbers, one decision

| Method | What it is | Failure mode |
|---|---|---|
| median of RR | the reported HR | wrong if peaks are doubled/missing |
| autocorrelation | dominant periodicity | can lock onto 2× period *with high correlation* |
| independent counter | 5–25 Hz envelope + peaks | can count T waves |

The count is accepted when it agrees within ±15 % with **at least one** reference
(autocorrelation if credible, otherwise the independent counter). If it agrees with neither,
rhythm and morphology are not interpreted and the report says so.

## 4. Median beat and interval measurement

Intervals are measured on the **median beat** of the 0.5–75 Hz signal (the 40 Hz low-pass used
for detection artificially widens QRS; `dwt` on single beats tends to include neighbouring
waves — on one recording it reported QRS 162 ms where the median beat gave ~57 ms).

| Interval | How | Rules that matter |
|---|---|---|
| QRS | widest continuous dV/dt block around R, gaps ≤ 30 ms merged | onset/offset from the derivative, not Q→S peaks |
| P | max in `[QRS onset − 260 ms, QRS onset − 40 ms]` | baseline from the **PR segment** (QRS onset −55…−25 ms); 15 % amplitude for onset/offset |
| P acceptance | `present` requires amp > 2 × local noise, width 35–120 ms, peak not at the window edge | a P wider than 120 ms is drift or fusion with T/U |
| PR | QRS onset − P onset | interpreted only when stable (see §5) |
| T / QT | tangent on the descending limb, after the T peak | fixed threshold cuts the T early and gives an unrealistically short QT |
| QTc | Bazett + Fridericia with the RR **preceding** the beat | reported as a measurement, not as an interpretation (see §7) |

Two systematic biases were found and fixed here, both worth knowing if you change the filter:

1. **Baseline taken at the start of the P window** sat on the slow ramp left by the 0.5 Hz
   high-pass after a large R wave → amplitude inflated (57 instead of 38 ADC) and onset moved
   125 ms to the left → **PR 224 ms ("first-degree AV block") on a recording whose PR is
   ~140 ms**.
2. **Baseline window overlapping the Q wave** (QRS onset −50…−10 ms) drags the baseline down
   for narrow QRS → PR overestimated by ~25 ms (synthetic 180 ms came out as 208 ms).

Additional guards: at HR > 105/min the previous T wave enters the P window (P-on-T), so PR is
**not** interpreted; and limits are never applied more strictly than the measurement
granularity (one sample = 5.2 ms at 192 Hz, so the classification tolerance is ±5 ms).

## 5. Consensus: is the measurement stable?

A single median beat is **one** estimate; nothing in it says whether it is reproducible. The
engine therefore repeats the same measurement on

- **4 partitions of the beats** (works on a 60 s recording), and
- **30 s segments** of the recording (≥ 3 segments, ≥ 90 s total),

and then applies a **spread test, not an average**:

| Parameter | Accept when |
|---|---|
| PR | IQR ≤ 20 ms **and** range ≤ 40 ms **and** ≥ 3 usable groups **and** (≥ 50 % of groups or ≥ 10 groups) |
| QRS | IQR ≤ 15 ms, range ≤ 30 ms |
| QT | IQR ≤ 25 ms, range ≤ 50 ms |

If it fails, the value is **not interpreted** — but the spread is printed (e.g. "PR measured
100–224 ms across groups"), because that spread is exactly the diagnostic information. Segment
consensus also flags a **state change** (HR range > 20 %), where a whole-recording median beat
mixes two morphologies and the correct tool is `tools/ecg_phases.py`.

⚠ Consensus removes **random** error, not **systematic** bias: every group would agree on the
same wrong number (that is exactly what happened with the baseline bug above). Fix the bias
first, then require agreement.

## 6. HRV

Task Force (1996) rules are followed: 5-minute short-term recordings (shorter ones are labelled
"orientational"), RR tachogram interpolated at 4 Hz, Welch spectrum with the standard bands,
artifacts/ectopics removed before analysis, and a MAD floor (2 % in log space) in the RR
outlier filter so that a very regular recording is not rejected as artefact.

Deliberately **not** claimed:

- **VLF** from a short recording is not interpretable — `vlf_ms2` is `null` below 10 min;
- **LF/HF** is a power ratio, **not** a measure of sympatho-vagal balance (Billman 2013) — it
  is shown as "ratio, orientational".

## 7. What is interpreted, and what is only measured

| Reported as a finding | Measured, no conclusion drawn |
|---|---|
| heart rate (two-method check) | QRS duration — single-lead measurement is systematically shorter than the global (12-lead) QRS, so values around 55–65 ms are a method floor, not a narrow QRS |
| RR regularity, ectopics, pauses | QT/QTc — tangent and beat-to-beat methods differ by >100 ms here (literature allows ~10 ms), so no "normal/prolonged" verdict |
| PR interval and its classification (120–200 ms) when consensus passes | ST level — no mV calibration |
| respiration rate from slow baseline oscillations | axis, hypertrophy, infarction/scar, bundle-branch block type, WPW, arrhythmia type |

The report also carries an explicit list of what a single-lead recording cannot establish, and
the plain-language section states it without abbreviations.

## 8. How this maps to published standards

| Requirement (source) | Implementation |
|---|---|
| HR verified by a second method (AHA/ACCF/HRS Part I) | three methods, acceptance on agreement |
| PR = P onset → QRS onset; normal 120–200 ms, > 200 ms = first-degree AV block (AHA/ACCF/HRS; ACC/AHA/HRS bradycardia guideline 2018) | onset-based measurement on the median beat, ±5 ms tolerance |
| Measure on a representative beat, not one beat (AHA Part I) | median beat over 300+ beats |
| Intervals are "global" across leads; single-lead values are shorter (AHA Part I/III) | single lead only — stated as a limitation, no global claims |
| QRS measurement method must be validated (IEC 60601-2-25) | not yet validated against an annotated database → QRS reported without classification |
| Short-term HRV: 2–5 min, 4 Hz interpolation, artifact removal (Task Force 1996) | implemented, shorter records labelled orientational |
| Single-lead ECG is acceptable evidence for AF (ESC 2020), arrhythmia type needs more leads | irregular RR + absent P are reported; type is never claimed |
| Diagnostic ECG: fs ≥ 500 Hz, ≤ 5 µV/LSB, 1 mV calibration (IEC 60601-2-25) | 192 Hz, no mV calibration → screening/trend only |

Open items, tracked deliberately: external validation of PR/QRS against annotated databases
(e.g. LUDB for P/QRS boundaries, PhysioNet QT Database for QT), and hardware upgrade to a
24-bit ≥ 500 Hz front end (ADS1292R / ADS1298).

## 9. Testing without hardware

`tools/gen_test_ecg.py` generates synthetic recordings in the same CSV format with known
morphology (R, Q, T, P at a fixed offset), which is how detection and interval logic are
regression-tested:

```bash
python3 tools/gen_test_ecg.py regular 112 60 > /tmp/reg.csv     # 112 bpm, PR ≈ 180 ms
python3 tools/gen_test_ecg.py brady   48 60 > /tmp/brady.csv    # 48 bpm
python3 tools/gen_test_ecg.py af          60 > /tmp/af.csv      # irregular RR, no P wave
python3 tools/gen_test_ecg.py app         75 360 > /tmp/app21.csv  # app 2.1 format with pauses
```

Synthetic files at a high rate are a trap: the generator keeps the T wave at a fixed +350 ms,
so at 112 bpm the T merges with the next P — the engine correctly refuses to interpret PR
there. Keep that in mind when adding test cases.
