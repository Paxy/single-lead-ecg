#!/usr/bin/env python3
"""Analiza dugog snimka (5-30 min, Android app) — prekidi toka, puls po minutima, klizni 30-s
maksimum, HRV po prozorima, pauze van prekida toka, poređenje sa baznim snimcima.

Pokreći venv-om servisa NA SERVERU (your-server):
  /opt/ecg-venv/bin/python3 long_recording_analysis.py /tmp/snimak.csv [bazni1.csv bazni2.csv]

Zašto na serveru: skripta uvozi `ecg_analyze`/`ecg_medical` (path preko ECG_SERVICE_DIR), pa je
detekcija udara identična glavnom izveštaju servisa. Bazni snimci (mirovanje, bez supstance) daju
poređenje za tvrdnje tipa „puls 12-15 bpm viši od mirnog baznog“.
"""
import os
import sys

import numpy as np

FS = 192.0
SERVICE_DIR = os.environ.get("ECG_SERVICE_DIR", "/var/www/html/ecg")
sys.path.insert(0, SERVICE_DIR)

import ecg_analyze as EA          # noqa: E402
import ecg_medical as EM          # noqa: E402
import neurokit2 as nk            # noqa: E402


def gaps_in_stream(times, big_s=5.0):
    """Prekidi toka podataka (praznine u vremenskoj osi) = izgubljeni uzorci, NE srčane pauze."""
    if times is None or len(times) < 3:
        return []
    tt = np.asarray(times, dtype=float)
    dt = np.diff(tt)
    return [(float(tt[i] - tt[0]), float(dt[i])) for i in np.where(dt > big_s)[0]]


def beats(csv_path):
    """Isti lanac kao EA.analyse(): učitavanje -> ose -> čišćenje -> vrhovi -> korekcija."""
    sig, times, _meta = EA.load_signal(csv_path, FS)
    t, fs_meas = EA.build_time_axis(times, sig.size, FS)
    clean = nk.ecg_clean(sig, sampling_rate=FS, method="neurokit")
    ac = EA.hr_autocorr(clean, FS)
    rp, n_double, _ = EA.correct_double_counts(
        EA.detect_beats(clean, FS), clean, (ac["period_s"] * FS) if ac else None, trusted=bool(ac))
    return dict(sig=sig, t=t, fs_meas=fs_meas, rp=rp, n_double=n_double, ac=ac,
                rt=t[rp], gaps=gaps_in_stream(times))


def hrv_windows(d, windows=((0, 300), (300, 600), (600, 1800))):
    rt = d["rt"]
    out = []
    for a, b in windows:
        if a >= rt[-1]:
            continue
        sel = rt[(rt >= a) & (rt < b)]
        if len(sel) < 30:
            continue
        rr = np.diff(sel)
        ok = (rr > 0.30) & (rr < 2.0)
        rr = rr[ok]
        if rr.size < 20:
            continue
        h = EA.hrv_time(rr)
        out.append((a, b, 60.0 / np.median(rr), h.get("sdnn_ms"), h.get("rmssd_ms"), h.get("pnn50_pct")))
    return out


def main():
    if len(sys.argv) < 2:
        raise SystemExit("upotreba: long_recording_analysis.py <snimak.csv> [bazni.csv ...]")
    d = beats(sys.argv[1])
    t, rt = d["t"], d["rt"]
    dur = float(t[-1])

    print(f"snimak: {sys.argv[1]}")
    print(f"uzoraka {d['sig'].size}  trajanje {dur:.0f} s ({dur / 60:.1f} min)  "
          f"fs izmerena {d['fs_meas']:.2f} Hz")
    print(f"R vrhova {len(rt)}" + (f" (uklonjeno {d['n_double']} lažnih)" if d['n_double'] else ""))
    if d["gaps"]:
        print("PREKIDI TOKA PODATAKA (nisu srčane pauze): "
              + ", ".join(f"na {g / 60:.2f} min +{s:.0f} s" for g, s in d["gaps"]))
        lost = sum(s for _g, s in d["gaps"])
        print(f"  izgubljeno ~{lost:.0f} s ({lost / dur * 100:.1f} % snimka); "
              f"nominalno bi trebalo {dur * FS / 60:.0f} uzoraka... "
              f"(stvarno {d['sig'].size + 1}, manjak {dur * FS - d['sig'].size:.0f})")

    print("\nPULS PO MINUTIMA")
    hrs = []
    for m in range(int(dur // 60) + 1):
        sel = rt[(rt >= 60 * m) & (rt < 60 * (m + 1))]
        if len(sel) < 5:
            print(f"  {m:02d} min: premalo udara ({len(sel)}) — prekid toka/pokret")
            continue
        hr = 60.0 / np.median(np.diff(sel))
        hrs.append(hr)
        print(f"  {m:02d} min: {hr:5.1f} bpm  " + "#" * int(round((hr - 60) * 2)))
    if hrs:
        print(f"  min {min(hrs):.1f} / medijana {np.median(hrs):.1f} / max {max(hrs):.1f} bpm")

    print("\nKLIZNI 30-s PROZOR (korak 10 s)")
    wins = []
    for a in np.arange(0, max(0.0, dur - 30), 10):
        sel = rt[(rt >= a) & (rt < a + 30)]
        if len(sel) < 8:
            continue
        rr = np.diff(sel)
        rr = rr[(rr > 0.3) & (rr < 2.0)]
        if rr.size < 6:
            continue
        wins.append((float(a), 60.0 / np.median(rr)))
    if wins:
        top = sorted(wins, key=lambda w: -w[1])[:5]
        for a, hr in top:
            print(f"  {int(a) // 60:02d}:{int(a) % 60:02d}  {hr:5.1f} bpm")
        print(f"  prozora {len(wins)} · najviši 30-s puls {max(h for _a, h in wins):.1f} bpm · "
              f"prozora ≥100: {sum(1 for _a, h in wins if h >= 100)}")
        rr_all = np.diff(rt)
        print(f"  udara sa RR < 600 ms (puls >100): {int(np.sum(rr_all < 0.6))} · "
              f"sa RR < 500 ms (>120): {int(np.sum(rr_all < 0.5))}")

    print("\nHRV PO PROZORIMA (5 min)")
    for a, b, hr, sdnn, rmssd, pnn in hrv_windows(d):
        print(f"  {a // 60}-{b // 60} min: puls {hr:5.1f} | SDNN {sdnn:5.1f} | RMSSD {rmssd:5.1f} | "
              f"pNN50 {pnn:4.1f}%")

    # pauze: RR preko medijane, ali RR parovi koji prelaze prekid toka se IZbacuju
    rp = d["rp"]
    rr = np.diff(rt)
    bad = np.zeros(rr.size, dtype=bool)
    for g0, gs in d["gaps"]:
        bad |= (rt[:-1] < g0) & (rt[1:] > g0 + gs * 0.5)
    rr_ok = rr[~bad]
    med = float(np.median(rr_ok))
    long_rr = rr_ok[rr_ok > 1.6 * med]
    ect = EM.ectopics_and_pauses(rr_ok)
    print(f"\nRITAM: medijana RR {med * 1000:.0f} ms · preuranjeni udari "
          f"{ect.get('premature', 0)} ({ect.get('premature_pct', 0):.1f} %) · "
          f"pauze van prekida toka {len(long_rr)}"
          + (f" (najduži {long_rr.max() * 1000:.0f} ms)" if long_rr.size else ""))
    if d["gaps"]:
        print(f"        najduži RR uključujući prekide toka: {rr.max() * 1000:.0f} ms "
              f"(= izgubljeni uzorci, ne srčana pauza)")

    for path in sys.argv[2:]:
        try:
            b = beats(path)
            rrb = np.diff(b["rt"])
            rrb = rrb[(rrb > 0.3) & (rrb < 2.0)]
            h = EA.hrv_time(rrb)
            print(f"\nBAZNI {path}: puls {60.0 / np.median(rrb):5.1f} | SDNN {h.get('sdnn_ms', 0):5.1f} | "
                  f"RMSSD {h.get('rmssd_ms', 0):5.1f} | pNN50 {h.get('pnn50_pct', 0):4.1f}% | "
                  f"udara {len(b['rt'])}  (HRV iz kratkog snimka je samo orijentaciona)")
        except Exception as exc:                      # noqa: BLE001
            print(f"\nBAZNI {path}: nije pročitano ({exc})")


if __name__ == "__main__":
    main()
