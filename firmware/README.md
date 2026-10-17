# Firmware — AD8232 sampler

`ecg_ad8232/ecg_ad8232.ino` reads the AD8232 `OUTPUT` pin and prints **one integer per line**

```
9600 baud, 8N1, "\r\n", values 0…1023
```

which is exactly what the browser plotter (`web/plotter.html`) and the Android app
(`android/`) expect. No header, no timestamps — the recorder stamps each sample on arrival.

## Flash

Arduino IDE → open `ecg_ad8232/ecg_ad8232.ino` → board: *Arduino Nano* (or your board) →
upload. Then check the port from a terminal:

```bash
screen /dev/ttyUSB0 9600      # numbers should scroll at ~190 lines/s; Ctrl-A K to quit
```

`screen` defaults to 9600 baud — that is the sanity check that both the firmware and the cable
are fine. If you see binary garbage, the firmware was built for a different baud rate.

## Why the loop is paced the way it is

- 9600 baud carries ~960 bytes/s; a 5-byte line (`512\r\n`) therefore limits the rate to
  **~192 samples/s**. The loop waits for the UART to have room (`Serial.availableForWrite()`)
  instead of blocking forever inside `Serial.print`, so the pacing follows the link.
- A 4-digit sample (`1023\r\n`) costs one extra byte, which slightly lowers the sustained rate.
  Keep the analog gain moderate (R wave ≈ ⅓–½ of the ADC range) — see `docs/hardware.md` §5.
- If you raise `BAUD`, you must also raise the nominal sample rate in the recorder UI and in
  the analysis call; otherwise intervals are computed on a wrong time base.

## What the firmware deliberately does not do

- **No filtering.** On-board AD8232 filters (0.5 Hz HP, ~40 Hz LP) are enough for acquisition;
  any further filtering belongs in the analysis (and the plotter filter is display-only).
- **No markers.** `# PAUSE`, `# MARK`, `# Q` lines are produced by the recorder (browser or
  Android app), which knows what the user was doing; the firmware has no notion of context.
- **No lead-off flag in the stream.** `LO+`/`LO−` pins are wired but not transmitted — the
  recorder infers contact quality from the signal itself (flat line, saturation, noise).
  Adding a text token would break the "one number per line" contract; if you need it, send it
  as a separate line starting with `#` and make the parser skip it.
