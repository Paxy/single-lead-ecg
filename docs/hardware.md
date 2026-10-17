# Hardware — single-lead ECG front end (AD8232)

> ⚠️ **Not a medical device.** This is a hobby/research acquisition chain. It is not
> certified, not calibrated in millivolts, and must never be used to diagnose, to decide
> therapy, or on another person. Battery power only while electrodes are on the body.

## 1. What the chain is

```
 Ag/AgCl electrodes            AD8232 module                    MCU + USB-serial
 (RA / LA / RLD)  ──────►  instrumentation amp + filters  ──►  A0, 10-bit ADC
                           OUTPUT 0…VS (≈0…3.3 V)              9600 baud ASCII
                                                                  ↓
                                                          one integer per line
                                                                  ↓
                                                    browser (Web Serial) or Android app
                                                                  ↓
                                                    server: PHP API → Python analysis
```

One differential lead, two signal electrodes plus one RLD (right-leg drive) electrode.
This is **not** a 12-lead ECG: no chest leads, no electrical axis, no mV calibration.

## 2. Bill of materials

| Part | Note |
|---|---|
| AD8232 ECG module (CJMCU-8232 or similar) | on-board 0.5 Hz high-pass, ~40 Hz low-pass, RLD amplifier |
| MCU board with 10-bit ADC + USB-serial | Arduino Nano/Uno class with CH340 (9600 baud works out of the box) or ESP32 |
| 3 × Ag/AgCl disposable electrodes with snap connector | signal–signal–RLD |
| 3-lead electrode cable with snap ends | keep it short; long unshielded leads pick up mains |
| battery pack (USB power bank) | see safety below |
| (optional) 3D-printed electrode-position template | for repeatable placement between sessions |

## 3. Wiring

| AD8232 pin | To | Note |
|---|---|---|
| `3V3` / `5V` | MCU supply (same rail as ADC reference) | module works from 3.3 V; keep ADC ref = supply |
| `GND` | MCU GND | single ground point |
| `OUTPUT` | `A0` | analog input, 0…VS |
| `LO+` | `D10` (optional) | leads-off detect, active high |
| `LO-` | `D11` (optional) | leads-off detect, active high |
| `SDN` | `3V3` (or MCU pin) | shutdown, active low — tie high to run |
| `IN+` / `IN-` | LA / RA electrodes | differential input |
| `RLD` | RLD electrode (right leg / lower right torso) | driven reference, reduces common-mode noise |

Electrode placement used in this project: **LA** on the left chest below the clavicle,
**RA** on the right chest mirrored, **RLD** on the lower right abdomen. Keep placement
identical between sessions if you compare shapes.

## 4. Sampling — why 192 Hz

The firmware sends one ASCII sample per line at **9600 baud, 8N1**. A line like `512\r\n`
is 5 bytes, so the serial link physically caps the rate at about **9600 / 5 ≈ 192 lines/s**.
Timestamps are assigned per sample on arrival, so the server reconstructs the time axis from
the timestamps, not from a nominal rate — but the recorder must be given the same nominal
rate (default 192 Hz) because filtering and window lengths use it.

Consequences, stated plainly:

- Resolution in time is **5.2 ms per sample** (2.6 ms with interpolation between samples).
  That is the floor for every interval this project reports.
- A 4-digit value (`1023\r\n`) is 6 bytes, which *lowers* the sustainable rate. Keep the
  analog gain such that the R wave stays in the middle of the range (see §5).
- Increasing the baud rate (19200/38400) lifts the ceiling, but the whole chain (firmware,
  recorder UI's nominal fs, and the analysis) must be changed together.

## 5. Gain, saturation and the "top of the rail" problem

The module's default gain is high enough that a normal R wave can reach the ADC rail
(0 or 1023). In practice the R wave sat far too high on this build: about 1.7 % of samples
sat at the rail and the median peak-to-peak amplitude was ~800 of 1023.

Symptoms of too much gain:

- flat-topped R waves, `pp` close to the full range, `clipped_pct` > 0.5 % in the report;
- the detector starts losing peaks, and heart rate can be reported at half its true value;
- the auto-pause logic in the recorder opens gaps where the signal saturates.

Fix: **reduce module gain (change the gain resistor) or move electrodes slightly**, so that
the R wave occupies roughly ⅓–½ of the ADC range (`pp` ≈ 300–500).

## 6. Calibration (what is missing)

There is **no 1 mV calibration** in this chain: amplitudes are ADC units, not millivolts.
That is why the report never makes ST-segment or voltage claims. To add it:

1. Feed a 1 mV rectangular pulse (ECG simulator such as SKX-2000C, or a resistive divider
   from a known reference) into the input;
2. record it, compute ADC counts per mV;
3. store that constant per device and convert amplitudes before any mV-based statement.

Until then: **amplitudes are relative only, and ST analysis is out of scope.**

## 7. Noise, drift and what the software does about it

| Problem | Cause | Mitigation in this project |
|---|---|---|
| baseline wander | respiration, electrode half-cell drift | 0.5 Hz high-pass in analysis; wander % reported |
| mains hum | unshielded leads, charger nearby | keep leads short, no charger, `powerline_pct` reported |
| motion artefacts | cable movement | recorder auto-pauses on flat/saturated/noisy signal and marks `# PAUSE` in the CSV |
| electrode loss | dry skin, poor adhesion | clean/dry skin, RLD electrode, `LO+/LO-` pins can be wired for a hardware flag |
| saturation | too much gain | see §5 |

A high-pass at 0.5 Hz leaves a slow ramp after every large R wave. That ramp used to be
mistaken for the P wave by the interval measurement (the baseline was taken at the start of
the P search window). The measurement now takes its baseline from the **PR segment** —
worth remembering if you change the filter: lower high-pass (0.05 Hz) makes this cleaner,
higher makes it worse.

## 8. If you need real morphology (and what it costs)

Everything this chain cannot do (axis, hypertrophy, infarction, mV-level ST, bundle-branch
block type, WPW, QT for drug decisions) needs **more leads and a calibrated front end**:

| Option | Channels | Resolution | Notes |
|---|---|---|---|
| ADS1292R module | 2 | 24-bit, ≥500 Hz | one step up: two simultaneous leads, internal test signal for calibration, lead-off detection; needs an SPI driver |
| ADS1298 (8 ch) | 8 | 24-bit, 500–1000 Hz | true 12-lead (3 limb + 6 chest + reference) with RLD/WCT; a ready-made board is the practical route |
| second AD8232 module | 2 | 10-bit, 192 Hz | cheapest experiment; one module must drive RLD only, both need a common reference and a faster baud rate |

Diagnostic-grade ECG equipment is specified by **IEC 60601-2-25** (sampling ≥ 500 Hz,
resolution ≤ 5 µV/LSB at the input, internal noise ≤ 30 µV, validation against annotated
databases). This project's chain does not meet the sampling requirement, so its output is a
screening/trend signal, never a diagnostic recording.
