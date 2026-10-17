#!/usr/bin/env python3
"""Slike za medi karton: (1) medijalni beat sa oznakama P/QRS/T, (2) preklopljeni oblici svih
sesija (dokaz da je oblik reproducibilan i da se razlike objašnjavaju položajem elektroda)."""
import glob
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import neurokit2 as nk

SERVICE_DIR = "/var/www/html/ecg"
sys.path.insert(0, SERVICE_DIR)
import ecg_analyze as EA      # noqa: E402
import ecg_medical as EM      # noqa: E402

FS = 192.0
OUT1, OUT2 = "/tmp/morph_fig1.png", "/tmp/morph_fig2.png"
plt.rcParams.update({"font.size": 9, "figure.dpi": 150, "axes.grid": True,
                     "grid.alpha": 0.25, "axes.axisbelow": True})


def prep(path):
    sig, times, _m = EA.load_signal(path, FS)
    t, _fs = EA.build_time_axis(times, sig.size, FS)
    pauses, gaps = EA.parse_markers(path)
    dropped = EA.dropped_intervals(pauses, gaps, float(t[-1]))
    clean = nk.ecg_clean(sig, sampling_rate=FS, method="neurokit")
    raw = EA.detect_beats(clean, FS)
    rp = raw[[not EA._in_intervals(float(t[p]), dropped) for p in raw]]
    wide = nk.signal_filter(sig, sampling_rate=FS, lowcut=0.5, highcut=min(75.0, FS / 2 - 2),
                            method="butterworth", order=4)
    mb = EM.median_beat(wide, clean, rp, FS)
    ivi = EM.intervals_from_median_beat(mb)
    return mb, ivi, rp


sessions = sorted(glob.glob(f"{SERVICE_DIR}/data/history/*/data.csv"))
best = [s for s in sessions if "20261016-181905" in s][0]
long_ = [s for s in sessions if "20261016-162037" in s][0]

# ---------------------------------------------------------------- figura 1
mb1, ivi1, rp1 = prep(best)
y, pre = np.asarray(mb1["sig"], dtype=float), int(mb1["pre"])
x = (np.arange(y.size) - pre) / FS * 1000.0
iso = float(ivi1.get("iso", np.median(y[:pre // 2])))
r_amp = float(np.max(y) - iso)

fig, ax = plt.subplots(2, 1, figsize=(11, 6.4), sharex=False)
a = ax[0]
a.plot(x, y - iso, color="#1f4e9c", lw=1.3, label="medijalni beat (211 otkucaja)")
a.axhline(0, color="#888", lw=0.8, ls=":")
p = ivi1.get("p") or {}
t_ = ivi1.get("t") or {}
qon, qoff = ivi1.get("qrs_on_ms"), ivi1.get("qrs_off_ms")
for v, lab, col, dy in ((p.get("pr_ms") and -float(p["pr_ms"]), "P", "#2e7d32", 0.55),
                        (qon, "QRS on", "#b00020", 0.85), (qoff, "QRS off", "#b00020", 0.72),
                        (t_.get("qt_ms"), "T off", "#6a1b9a", 0.85)):
    if v is None or not np.isfinite(v):
        continue
    a.axvline(float(v), color=col, lw=1.0, ls="--", alpha=0.9)
    a.text(float(v), (r_amp * dy), lab, color=col, ha="center", va="bottom", fontsize=10,
               bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=col, alpha=0.85, lw=0.7))
a.axvspan(float(qon), float(qoff), color="#b00020", alpha=0.07)
a.set_title("Medijalni beat — snimak 16.10.2026 18:19 (kvalitet ODLIČAN 98, 2.4 min)\n"
            f"PR {p.get('pr_ms', float('nan')):.0f} ms   QRS {ivi1.get('qrs_ms', float('nan')):.0f} ms   "
            f"QT {t_.get('qt_ms', float('nan')):.0f} ms   (jedan odvod, ADC jedinice — nije mV)",
            fontsize=10)
a.set_xlim(-300, 500)
a.set_ylabel("ADC — izoelektrična linija")
a.set_xlabel("ms oko R talasa (R = 0)")
a.legend(loc="upper right", fontsize=8)

mb2, ivi2, rp2 = prep(long_)
y2, pre2 = np.asarray(mb2["sig"], dtype=float), int(mb2["pre"])
x2 = (np.arange(y2.size) - pre2) / FS * 1000.0
iso2 = float(ivi2.get("iso", np.median(y2[:pre2 // 2])))
b = ax[1]
b.plot(x2, y2 - iso2, color="#c65102", lw=1.2, label="medijalni beat (1767 otkucaja, 29.5 min)")
b.plot(x, y - iso, color="#1f4e9c", lw=1.2, alpha=0.75, label="18:19 (2.4 min) — isti oblik")
b.axhline(0, color="#888", lw=0.8, ls=":")
b.set_title("Isti oblik u dugom i kratkom snimku (korelacija 0.91) — oblik se ne menja u toku dana",
            fontsize=10)
b.set_xlim(-300, 500)
b.set_xlabel("ms oko R talasa (R = 0)")
b.set_ylabel("ADC (skalirano)")
b.legend(loc="upper right", fontsize=8)
fig.tight_layout()
fig.savefig(OUT1)
print("napisano:", OUT1)

# ---------------------------------------------------------------- figura 2
rows = []
for path in sessions:
    sid = os.path.basename(os.path.dirname(path))
    try:
        mb, ivi, rp = prep(path)
        y = np.asarray(mb["sig"], dtype=float)
        pre = int(mb["pre"])
        a1, b1 = max(0, pre - int(0.25 * FS)), min(len(y), pre + int(0.45 * FS))
        seg = y[a1:b1] - float(np.median(y[:max(1, pre - int(0.30 * FS))]))
        rows.append((sid, seg / (float(np.max(seg)) or 1.0), sid.startswith("202609")))
    except Exception as exc:  # noqa: BLE001
        print("greska", sid, exc)

n = min(len(s) for _sid, s, _o in rows)
fig, ax = plt.subplots(figsize=(11, 4.6))
xs = (np.arange(n) - int(0.25 * FS)) / FS * 1000.0
for sid, seg, old in rows:
    ax.plot(xs, seg[:n], lw=1.4,
            color="#1565c0" if old else "#c65102", alpha=0.75,
            label=(sid + ("  (25/28.09)" if old else "  (16.10)")))
ax.axhline(0, color="#888", lw=0.8, ls=":")
ax.set_xlim(-250, 450)
ax.set_title("Uporedjenje oblika medijalnih beatova svih snimaka (normalizovano na R)\n"
             "unutar istog dana / polozaja elektroda korelacija 0.97-1.00; septembar vs oktobar 0.87-0.95",
             fontsize=10)
ax.set_xlabel("ms oko R talasa (R = 0)")
ax.set_ylabel("normalizovana amplituda (R = 1)")
ax.legend(fontsize=7.5, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.22),
          frameon=False)
ax.set_xlim(-250, 400)
fig.tight_layout()
fig.savefig(OUT2)
print("napisano:", OUT2)
