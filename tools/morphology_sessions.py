#!/usr/bin/env python3
"""Morfologija kroz SVE sesije servisa (jednokanalni snimak): medijalni beat po sesiji, mere
P/QRS/T, stabilnost oblika i medjusobna slicnost oblika (dokaz da li su snimci uporedivi).

Pokretanje NA SERVERU (venv servisa), iz /var/www/html/ecg:
  scp scripts/morphology_sessions.py root@your-server:/tmp/
  ssh root@your-server 'cd /var/www/html/ecg && \
      /opt/ecg-venv/bin/python3 /tmp/morphology_sessions.py [--ascii]'

Zasto ovako: izvestaj servisa daje JEDAN nalaz za JEDAN snimak, a pitanje „sta se vidi o srcu /
da li se oblik menja" trazi isti motor preko svih sesija + dokaz da je polozaj elektroda isti.
Pravila tumacenja (granularnost mera, ST60 nije ST nalaz, sta se ne sme tvrditi):
references/ecg-analysis-metrics.md, sekcija „Morfologija kroz vise snimaka".
"""
import glob
import os
import sys

import numpy as np
import neurokit2 as nk

SERVICE_DIR = os.environ.get("ECG_SERVICE_DIR", "/var/www/html/ecg")
sys.path.insert(0, SERVICE_DIR)
import ecg_analyze as EA      # noqa: E402
import ecg_medical as EM      # noqa: E402

FS = 192.0
DO_ASCII = "--ascii" in sys.argv


def ascii_profile(y, pre, fs, width=78):
    """ASCII medijalnog beata (-0.30 s .. +0.50 s oko R); 1 znak = step ms."""
    a = max(0, pre - int(0.30 * fs))
    b = min(len(y), pre + int(0.50 * fs))
    seg = y[a:b]
    step = max(1, seg.size // width)
    cols = [seg[i:i + step] for i in range(0, seg.size, step)]
    lo, hi = float(np.min(seg)), float(np.max(seg))
    levels = 12
    grid = [[" "] * len(cols) for _ in range(levels)]
    for c, col in enumerate(cols):
        idx = int(round((float(np.mean(col)) - lo) / max(1e-9, hi - lo) * (levels - 1)))
        grid[levels - 1 - idx][c] = "#"
    rcol = int((pre - a) / step)
    out = ["  |" + "".join(r) + "|" for r in grid]
    out.append("  " + " " * (rcol + 3) + "^ R" +
               f"   (levo -0.30 s, R u sredini, desno +0.50 s; opseg {lo:.0f}-{hi:.0f} ADC; "
               f"1 znak = {step / fs * 1000:.0f} ms)")
    return out


def session_beats(path):
    """Isti lanac kao motor: ucitanje -> osa -> markeri -> ciscenje -> vrhovi -> korekcija."""
    sig, times, _meta = EA.load_signal(path, FS)
    t, fs_meas = EA.build_time_axis(times, sig.size, FS)
    pauses, gaps = EA.parse_markers(path)
    dropped = EA.dropped_intervals(pauses, gaps, float(t[-1]))
    keep = np.ones(sig.size, dtype=bool)
    for a, b in dropped:
        keep &= ~((t >= a) & (t < b))
    clean = nk.ecg_clean(sig, sampling_rate=FS, method="neurokit")
    ac = EA.hr_autocorr(clean, FS)
    raw = EA.detect_beats(clean, FS)
    raw = raw[[not EA._in_intervals(float(t[p]), dropped) for p in raw]]
    ac_ok = bool(ac and ac.get("ok") and ac.get("peak_corr", 0) >= EA.AC_MIN_PEAK_CORR)
    rp, n_double, _rev = EA.correct_double_counts(
        raw, clean, (ac["period_s"] * FS) if ac else None, trusted=ac_ok)
    ind = EA.independent_hr(sig, t, FS)
    hr_ind = float(ind["hr_bpm"]) if ind else None
    if n_double and hr_ind and len(rp) > 2:
        hr_after = float(np.median(60.0 / np.diff(t[rp])))
        if 0.35 <= hr_after / hr_ind <= 0.65:
            rp = raw                      # okttavna greska -> vrati original
    return sig, t, keep, clean, rp


rows, shapes, ids = [], [], []
for path in sorted(glob.glob(f"{SERVICE_DIR}/data/history/*/data.csv")):
    sid = os.path.basename(os.path.dirname(path))
    try:
        sig, t, keep, clean, rp = session_beats(path)
        rr = np.diff(t[rp])
        rr = rr[(rr > 0.25) & (rr < 2.0)]
        hr = 60.0 / np.median(rr) if rr.size else float("nan")
        rr_med = float(np.median(rr)) if rr.size else float("nan")
        wide = nk.signal_filter(sig, sampling_rate=FS, lowcut=0.5,
                                highcut=min(75.0, FS / 2 - 2), method="butterworth", order=4)
        mb = EM.median_beat(wide, clean, rp, FS)
        ivi = EM.intervals_from_median_beat(mb) if mb else {}
        y, pre = (mb["sig"], int(mb["pre"])) if mb else (None, 0)
        row = dict(id=sid, dur=float(t[-1] - t[0]), hr=hr, beats=len(rp),
                   corr=(mb or {}).get("template_corr_median"),
                   dropped=100 * (1 - keep.mean()), r_amp=None, qrs=None, pr=None, qt=None,
                   qtcf=None, p_snr=None, p_present=None, t_ratio=None, t_min=None)
        if mb is not None and ivi:
            iso = float(ivi.get("iso", np.median(y[:max(1, pre - int(0.3 * FS))])))
            r_amp = float(np.max(y) - iso)
            p = ivi.get("p") or {}
            tt = ivi.get("t") or {}
            row.update(r_amp=r_amp, qrs=ivi.get("qrs_ms"), pr=p.get("pr_ms"),
                       p_snr=p.get("snr"), p_present=p.get("present"),
                       qt=tt.get("qt_ms"), t_ratio=(tt.get("amp") / r_amp) if tt.get("amp") else None)
            if row["qt"] and np.isfinite(rr_med):
                row["qtcf"] = float(row["qt"]) / (rr_med ** (1.0 / 3.0))
            w0, w1 = pre + int(0.15 * FS), min(len(y), pre + int(0.45 * FS))
            if w1 > w0:
                row["t_min"] = float(np.min(y[w0:w1]) - iso) / max(1e-9, r_amp)
            a3, b3 = max(0, pre - int(0.25 * FS)), min(len(y), pre + int(0.45 * FS))
            seg = y[a3:b3] - float(np.median(y[:max(1, pre - int(0.30 * FS))]))
            shapes.append(seg / (float(np.max(seg)) or 1.0))
            ids.append(sid)
        rows.append(row)
    except Exception as exc:  # noqa: BLE001
        rows.append(dict(id=sid, error=str(exc)[:90]))

print(f"{'sesija':18s} {'min':>5s} {'HR':>5s} {'udara':>5s} {'Ramp':>5s} {'QRS':>5s} {'PR':>5s} "
      f"{'P?':>3s} {'Psnr':>5s} {'QT':>5s} {'QTcF':>5s} {'T/R':>6s} {'Tmin/R':>7s} {'obl':>5s} {'odb%':>5s}")
for r in rows:
    if r.get("error"):
        print(f"{r['id']:18s}  GRESKA: {r['error']}")
        continue

    def f(v, w=5, d=1):
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return "-".rjust(w)
        return f"{v:{w}.{d}f}"

    print(f"{r['id']:18s} {r['dur'] / 60:5.1f} {f(r['hr'])} {r['beats']:5d} {f(r['r_amp'], 5, 0)} "
          f"{f(r['qrs'], 5, 0)} {f(r['pr'], 5, 0)} {('DA' if r['p_present'] else 'ne'):>3s} "
          f"{f(r['p_snr'])} {f(r['qt'], 5, 0)} {f(r['qtcf'], 5, 0)} {f(r['t_ratio'], 6, 2)} "
          f"{f(r['t_min'], 7, 2)} {f(r['corr'], 5, 2)} {f(r['dropped'])}")

if len(shapes) > 1:
    n = min(len(s) for s in shapes)
    M = np.array([s[:n] for s in shapes])
    print("\nSLICNOST OBLIKA (korelacija normalizovanih medijalnih beatova; >=0.95 = isti polozaj "
          "elektroda, <=0.9 = drugi odvod):")
    print("                   " + " ".join(f"{i:>5d}" for i in range(len(ids))))
    for i in range(len(ids)):
        cs = []
        for j in range(len(ids)):
            a, b = M[i], M[j]
            c = np.corrcoef(a, b)[0, 1] if a.std() > 0 and b.std() > 0 else float("nan")
            cs.append(f"{c:5.2f}")
        print(f"{i:>2d} {ids[i]:15s} " + " ".join(cs))
    for i, sid in enumerate(ids):
        print(f"  [{i}] {sid}")

if DO_ASCII:
    print("\nMEDIJALNI BEAT (ASCII) — poslednje 3 sesije:")
    for path in sorted(glob.glob(f"{SERVICE_DIR}/data/history/*/data.csv"))[-3:]:
        sid = os.path.basename(os.path.dirname(path))
        try:
            sig, t, keep, clean, rp = session_beats(path)
            wide = nk.signal_filter(sig, sampling_rate=FS, lowcut=0.5,
                                    highcut=min(75.0, FS / 2 - 2), method="butterworth", order=4)
            mb = EM.median_beat(wide, clean, rp, FS)
            print(f"\n{sid}  (beats {rp.size})")
            for line in ascii_profile(mb["sig"], int(mb["pre"]), FS):
                print(line)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sid}: {exc}")
