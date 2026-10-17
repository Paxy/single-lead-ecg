#!/usr/bin/env python3
"""TRI NEZAVISNA BROJACA UDARA + RR statistika na AKTIVNIM segmentima (bez PAUSE/GAP).

Obavezna kontrola posle svake izmene detekcije i pre svake tvrdnje o pulsu/ritmu na
snimku iz Android aplikacije (koja pise # PAUSE/# RESUME/# GAP markere).

Pokretanje NA SERVERU:
  scp ecg_crosscheck.py root@your-server:/tmp/
  ssh root@your-server 'cd /var/www/html/ecg && \
    /opt/ecg-venv/bin/python3 /tmp/ecg_crosscheck.py /tmp/snimak.csv [jos.csv ...]'

Zasto tri detektora: `ecg_analyze` ume da napravi OKTAVNU GRESKU (prijavi ~pola pulsa)
kad autokorelacija uhvati 2x period a `correct_double_counts` na osnovu nje pobrise svaki
drugi pravi udar. Servisov `ac["ok"]` se NE sme koristiti kao dokaz: u praksi je
`trusted=bool(ac)` propustio referencu sa peak_corr 0.17 i `ok=False`.
Ako se broj udara servisa i broj iz ovog skripta razlikuju ~2x (ili >20 %) — nalaz servisa
NIJE za upotrebu dok se ne pogleda amplituda uklonjenih vrhova (odnos blizu 1.0 = pravi R).
"""
import os
import re
import sys

import numpy as np
import neurokit2 as nk
from scipy.signal import find_peaks, butter, filtfilt

SERVICE_DIR = os.environ.get("ECG_SERVICE_DIR", "/var/www/html/ecg")
sys.path.insert(0, SERVICE_DIR)
import ecg_analyze as EA  # noqa: E402

FS = 192.0


def secs(x):
    m, s = x.split(":")
    return int(m) * 60 + int(s)


def markers(path):
    """vraca (pauze [(t0,t1,label)], praznine [(t0, trajanje)]) iz # PAUSE/# RESUME/# GAP"""
    pauses, gaps, cur = [], [], None
    for line in open(path, errors="replace"):
        if line.startswith("# PAUSE"):
            cur = (secs(re.search(r"t=(\d+:\d+)", line).group(1)), line.split("label=")[-1].strip())
        elif line.startswith("# RESUME") and cur:
            pauses.append((cur[0], secs(re.search(r"t=(\d+:\d+)", line).group(1)), cur[1]))
            cur = None
        elif line.startswith("# GAP"):
            gaps.append((secs(re.search(r"t=(\d+:\d+)", line).group(1)),
                         float(re.search(r"s=([\d.]+)", line).group(1))))
    return pauses, gaps


def main(paths):
    for path in paths:
        pauses, gaps = markers(path)
        sig, times, _meta = EA.load_signal(path, FS)
        t, fs_meas = EA.build_time_axis(times, sig.size, FS)
        clean = nk.ecg_clean(sig, sampling_rate=FS, method="neurokit")
        ac = EA.hr_autocorr(clean, FS)
        raw = EA.detect_beats(clean, FS)
        rp, n_double, _ = EA.correct_double_counts(
            raw, clean, (ac["period_s"] * FS) if ac else None, trusted=bool(ac))

        # kontrolni detektor: band-pass 5-25 Hz + anvelopa
        b, a = butter(3, [5 / (FS / 2), 25 / (FS / 2)], btype="band")
        env = np.abs(filtfilt(b, a, sig))
        w = int(0.05 * FS)
        env = np.convolve(env, np.ones(w) / w, mode="same")
        pk, _ = find_peaks(env, distance=int(0.35 * FS), height=0.5 * np.percentile(env, 99))
        # treci: neurokit R vrhovi (indeksi iz info, NE kolona 0/1 iz DataFrame-a!)
        _, info = nk.ecg_peaks(clean, sampling_rate=FS)
        nk_idx = np.asarray(info["ECG_R_Peaks"])

        def bad(tv):
            m = np.zeros(tv.size, dtype=bool)
            for a2, b2, _l in pauses:
                m |= (tv >= a2) & (tv < b2)
            for g0, gl in gaps:
                m |= (tv >= g0) & (tv < g0 + gl)
            return m

        paused = sum(b2 - a2 for a2, b2, _l in pauses) + sum(g for _, g in gaps)
        print("=" * 78)
        print(f"{os.path.basename(path)} | t_axis {t[-1]:.0f} s | aktivno {t[-1]-paused:.0f} s "
              f"| pauze {len(pauses)} | praznine {len(gaps)}")
        print(f"  fs na aktivnom delu = {sig.size/(t[-1]-paused):.1f} Hz "
              f"(ako je ~nominalna, 'odstupanje fs' u izvestaju je artefakt pauza)")
        print(f"  autocorr: {ac['hr_bpm']:.1f} bpm (period {ac['period_s']*1000:.0f} ms, "
              f"peak_corr {ac['peak_corr']:.3f}, ok={ac['ok']})")
        amp_raw = np.abs(clean[np.asarray(raw)])
        kept = np.isin(np.asarray(raw), np.asarray(rp))
        print(f"  SERVIS: sirovo {raw.size} -> {rp.size} (uklonjeno {n_double}) | "
              f"amplituda uklonjenih/zadrzanih = "
              f"{np.median(amp_raw[~kept])/max(1e-9, np.median(amp_raw[kept])):.2f} "
              f"(>0.6 = uklonjeni su PRAVI R talasi -> okttavna greska)")
        for name, idx in (("anvelopa", pk), ("neurokit", nk_idx)):
            tv = t[idx]
            act = tv[~bad(tv)]
            rr = np.diff(act)
            rr = rr[(rr > 0.25) & (rr < 5.0)]
            if rr.size < 10:
                continue
            q = np.percentile(rr, [5, 25, 50, 75, 95]) * 1000
            print(f"  {name:9s}: {act.size} udara | HR {60/np.median(rr):.1f} bpm | "
                  f"RR p5 {q[0]:.0f} p25 {q[1]:.0f} med {q[2]:.0f} p75 {q[3]:.0f} p95 {q[4]:.0f} ms"
                  f" | >2 s: {(rr>2.0).sum()}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["/tmp/data.csv"])
