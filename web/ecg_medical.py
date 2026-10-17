#!/usr/bin/env python3
"""
Dodatne analize + medicinsko tumačenje za JEDNOKANALNI, NEKALIBRISANI ECG.

Princip: sve što se tvrdi mora biti izmereno na ovom snimku, a sve što se ne može
utvrditi jednim kanalom se eksplicitno kaže (nema izmišljanja nalaza).
"""
from __future__ import annotations

import numpy as np
from scipy import signal as sps

# --- referentne vrednosti (odrasli, mir, opšta populacija) -------------------
PR_TOL_MS = 5.0   # tačnost merenja PR-a na 192 Hz (jedan uzorak = 5.2 ms)
NORM = {
    "pr": (120, 200),        # ms
    "qrs": (60, 120),        # ms; >120 = širok
    "qtc_bazett": (350, 450),  # ms
    "hr_rest": (60, 100),    # /min
}


# ============================================================ 1. MEDIJALNI BEAT
def median_beat(sig_wide: np.ndarray, sig_narrow: np.ndarray | None, rp: np.ndarray,
                fs: float, pre_s: float = 0.45, post_s: float = 0.55) -> dict | None:
    """Medijalni beat: usrednjavanje otklanja šum, pa se P/QRS/T mere na njemu.

    sig_wide  — 0.5–75 Hz (za merenje trajanja; 40 Hz low-pass veštački širi QRS)
    sig_narrow— isto, uži opseg (za prikaz)
    """
    pre, post = int(pre_s * fs), int(post_s * fs)
    ok = rp[(rp > pre) & (rp < len(sig_wide) - post)]
    if ok.size < 5:
        return None
    M = np.array([sig_wide[r - pre:r + post] for r in ok], dtype=float)
    mb = np.median(M, axis=0)
    corr = np.array([np.corrcoef(row, mb)[0, 1] for row in M])
    out = {
        "sig": mb, "pre": pre, "fs": fs, "n_beats": int(ok.size),
        "template_corr_median": float(np.nanmedian(corr)),
    }
    if sig_narrow is not None:
        Mn = np.array([sig_narrow[r - pre:r + post] for r in ok], dtype=float)
        out["sig_narrow"] = np.median(Mn, axis=0)
    return out


def intervals_from_median_beat(mb: dict) -> dict:
    """QRS onset/offset (derivacija, kontinuirani segment oko R), P talas i T talas.

    Vraća sve u ms relativno na R vrh (R = 0). Otpornije na pojedinačne promašaje
    nego merenje beat-po-beat.
    """
    y = np.asarray(mb["sig"], dtype=float)
    pre, fs = int(mb["pre"]), float(mb["fs"])
    n = y.size
    d = np.abs(np.diff(y)) * fs                    # |dV/dt| u jed/s

    # --- QRS: najširi neprekinuti segment oko R gde je |dV/dt| iznad praga
    a, b = max(0, pre - int(0.15 * fs)), min(n - 1, pre + int(0.17 * fs))
    seg = d[a:b]
    if seg.size < 5 or not np.isfinite(seg).any():
        return {}
    peak = float(np.nanmax(seg))
    if peak <= 0:
        return {}
    out = {}
    # |dV/dt| je ~0 tačno u vrhu R, pa se ne sme tražiti "neprekidan blok oko R":
    # spajaju se blokovi nagiba (uzbrdo + nizbrdo) sa prekidom do 30 ms.
    def span(frac):
        above = np.where(seg > frac * peak)[0]
        if above.size == 0:
            return None
        runs = []
        start = prev = above[0]
        for k in above[1:]:
            if k - prev > max(1, int(0.03 * fs)):
                runs.append((start, prev))
                start = k
            prev = k
        runs.append((start, prev))
        m = int(np.argmax(seg))
        best = min(runs, key=lambda r: 0 if r[0] <= m <= r[1] else min(abs(r[0] - m), abs(r[1] - m)))
        merged = list(best)
        for r in runs:                                    # pripoji susedne blokove
            if r[1] < merged[0] and merged[0] - r[1] <= int(0.03 * fs):
                merged[0] = r[0]
            elif r[0] > merged[1] and r[0] - merged[1] <= int(0.03 * fs):
                merged[1] = r[1]
        return merged

    on_list, off_list = [], []
    for frac in (0.12, 0.18, 0.25):
        s = span(frac)
        if s:
            on_list.append((s[0] + a - pre) / fs * 1000.0)
            off_list.append((s[1] + a - pre) / fs * 1000.0)
    if not on_list:
        return {}
    qon, qoff = float(np.median(on_list)), float(np.median(off_list))
    out["qrs_on_ms"], out["qrs_off_ms"] = qon, qoff
    out["qrs_ms"] = qoff - qon

    # --- bazna linija (PR segment) i nivo šuma
    # Prozor je 55..25 ms pre QRS onseta: ranije je bio 50..10 ms, pa je kod uskog QRS-a
    # (Q talas na ~15 ms pre R) Q upadao u bazu i obarao je → P amplituda veštački veća i
    # PR duži (na sintetičkom snimku sa PR 180 ms izlazilo je 208 ms = lažni AV blok I).
    pq_a = max(0, pre + int((qon - 55) / 1000 * fs))
    pq_b = max(1, pre + int((qon - 25) / 1000 * fs))
    iso = float(np.median(y[pq_a:pq_b])) if pq_b > pq_a else float(np.median(y[:10]))
    noise = float(1.4826 * np.median(np.abs(y - np.median(y)))) or 1e-9
    out["iso"] = iso
    out["noise"] = noise

    # --- P talas: prozor 260..40 ms pre QRS onseta; baza iz PQ segmenta
    # 320 ms je hvatalo spor uspon koji ostaje od 0.5 Hz high-pass filtra (posledica velikog
    # R talasa), a baza uzeta sa POČETKA prozora je ležala upravo na tom usponu → amplituda
    # naduvana (57 umesto 38 ADC) i onset pomeren 125 ms ulevo. Posledica je bila PR 224 ms
    # ("AV blok I stepena") na snimku gde je PR 141 ms. Baza zato ide iz PQ segmenta, koji je
    # stvarna izoelektrična linija neposredno pre QRS-a.
    p_a = max(0, pre + int((qon - 260) / 1000 * fs))
    p_b = max(1, pre + int((qon - 40) / 1000 * fs))
    p = {}
    if p_b - p_a >= 5:
        segp = y[p_a:p_b]
        k = int(np.argmax(segp))
        base = iso                     # PQ segment (qon−50 … qon−10), vidi gore
        amp = float(segp[k] - base)
        # Lokalni šum P prozora (MAD). MAD celog otkucaja je naduvan QRS-om i T talasom, pa je
        # mali ali stvaran P talas ispadao „ispod šuma" (SNR 1.9 umesto ~4) i PR se blokirao.
        p_noise = float(1.4826 * np.median(np.abs(segp - np.median(segp)))) or 1e-9
        # onset/offset P talasa: gde se vraća na 15% amplitude
        lvl = base + 0.15 * amp
        i = p_a + k
        while i > p_a and y[i] > lvl:
            i -= 1
        j = p_a + k
        while j < p_b - 1 and y[j] > lvl:
            j += 1
        width = (j - i) / fs * 1000.0
        p = {
            "peak_ms": (p_a + k - pre) / fs * 1000.0,
            "on_ms": (i - pre) / fs * 1000.0,
            "off_ms": (j - pre) / fs * 1000.0,
            "amp": amp,
            "noise": p_noise,
            "snr": amp / p_noise if amp > 0 else 0.0,
            "pr_ms": (qon - (i - pre) / fs * 1000.0) if amp > 0 else np.nan,
            "width_ms": width,
            "base": base,
            "base_src": "pq",
            # P talas širi od 120 ms nije čist P talas (drift ili spoj sa T/U talasom — na
            # visokoj frekvenciji prethodni T ulazi u prozor), a vrh na ivici prozora je drift.
            "present": bool(amp > 2.0 * p_noise and 35.0 <= width <= 120.0
                            and (p_a + 5) < (p_a + k) < (p_b - 3)
                            and (p_a + k) < (pre + int(qon / 1000 * fs))),
        }
    out["p"] = p

    # --- T talas: 80..420 ms posle QRS offseta; kraj se traži tek POSLE vrha
    t_a = max(0, pre + int((qoff + 80) / 1000 * fs))
    t_b = min(n - 1, pre + int((qoff + 420) / 1000 * fs))
    t = {}
    r_amp = float(np.max(y) - iso) or 1e-9
    if t_b - t_a >= int(0.05 * fs):
        segt = y[t_a:t_b]
        k = int(np.argmax(segt))
        amp = float(segt[k] - iso)
        peak = t_a + k
        # kraj T talasa: tangenta na najstrmiju nizbrdicu posle vrha → presek sa
        # izoelektričnom linijom (standardna metoda; 15% prag ume da seče prerano)
        w = max(2, int(0.012 * fs))
        dn = peak + int(0.25 * fs)
        dn = min(dn, n - 2)
        j_end = None
        if dn - peak > 2 * w:
            slope_win = np.array([(y[i + w] - y[i - w]) / (2 * w / fs) for i in range(peak + w, dn - w)]) if dn - w > peak + w else np.array([])
            if slope_win.size:
                i_min = peak + w + int(np.argmin(slope_win))
                sl = slope_win.min()
                if sl < 0:
                    x = np.arange(i_min - w, i_min + w + 1)
                    coef = np.polyfit(x, y[i_min - w:i_min + w + 1], 1)
                    if coef[0] < 0:
                        x_cross = (iso - coef[1]) / coef[0]
                        if i_min < x_cross < min(n - 1, i_min + int(0.25 * fs)):
                            j_end = int(round(x_cross))
        if j_end is None:                             # rezervna metoda: 10% amplitude
            lvl = iso + 0.10 * amp
            j = peak
            while j < n - 2 and y[j] > lvl:
                j += 1
            j_end = j
        end_ms = (j_end - pre) / fs * 1000.0
        peak_ms = (peak - pre) / fs * 1000.0
        ok = (amp > 2.0 * noise and amp > 0.12 * r_amp
              and end_ms - peak_ms >= 50.0 and j_end < n - 3)
        t = {
            "peak_ms": peak_ms,
            "end_ms": end_ms,
            "amp": amp,
            "snr": amp / noise if amp > 0 else 0.0,
            "qt_ms": (end_ms - qon) if ok else None,
            "reliable": bool(ok),
            "method": "tangenta" if j_end is not None else "prag10%",
        }
    out["t"] = t
    out["iss"] = bool(p.get("present") and t.get("snr", 0) > 2)
    return out


# ================================================= 2. EKTOPIJE I PAUZE
def ectopics_and_pauses(rr: np.ndarray, rr_times: np.ndarray | None = None) -> dict:
    """Preuranjeni udari (RR kratak pa kompenzatorna pauza) i pauze.

    Ne tvrdi poreklo (atrijska/komorska) — jednokanalni snimak to ne može.
    """
    if rr.size < 6:
        return {"n": int(rr.size), "premature": 0, "pauses": 0, "longest_ms": None}
    med = float(np.median(rr))
    early = rr < 0.85 * med
    late = np.concatenate([rr[1:], [np.nan]])
    premature = int(np.sum(early & (late > 1.05 * med)))
    pauses = np.where(rr > 1.6 * med)[0]
    out = {
        "n": int(rr.size),
        "median_rr_ms": med * 1000,
        "premature": premature,
        "pauses": int(pauses.size),
        "longest_ms": float(np.max(rr)) * 1000,
        "rr_range_ms": [float(np.min(rr)) * 1000, float(np.max(rr)) * 1000],
    }
    if rr_times is not None and pauses.size:
        out["pause_times_s"] = [round(float(rr_times[i]), 1) for i in pauses[:10]]
    return out


# ================================================= 3. DISANJE (iz baseline-a)
def respiration_rate(sig: np.ndarray, fs: float) -> dict:
    """Frekvencija disanja iz sporih oscilacija signala (0.1–0.6 Hz)."""
    x = np.asarray(sig, dtype=float)
    x = x - np.mean(x)
    if x.size < int(10 * fs):
        return {}
    sos = sps.butter(3, [0.08, 0.65], btype="band", fs=fs, output="sos")
    resp = sps.sosfiltfilt(sos, x)
    nper = int(min(2048, resp.size))
    f, pxx = sps.welch(resp, fs=fs, nperseg=nper)
    band = (f >= 0.10) & (f <= 0.60)
    if band.sum() < 2 or not np.isfinite(pxx[band]).any():
        return {}
    k = int(np.argmax(pxx[band]))
    f_peak = float(f[band][k])
    # dominacija: udeo vrha u snazi opsega (trapz na jednom bin-u je 0, pa se
    # koristi zbir binova — rezolucija Welch-a je gruba za tako uzak opseg)
    tot = float(np.sum(pxx[band])) or 1e-12
    df = float(f[1] - f[0]) if f.size > 1 else 0.01
    near = (f >= f_peak - 1.5 * df) & (f <= f_peak + 1.5 * df)
    dom = float(np.sum(pxx[near])) / tot * 100
    return {
        "breaths_per_min": round(f_peak * 60, 1),
        "hz": round(f_peak, 3),
        "dominance_pct": round(dom, 1),
        "amp": float(np.std(resp)),
    }


# ================================================= 4. NELINEARNA HRV
def dfa_alpha1(rr: np.ndarray, n_min: int = 4, n_max: int = 16) -> float | None:
    """Detrended fluctuation analysis, kratkoročni eksponent α1.

    Standard (Task Force) traži ≥5 min; ovde je orijentaciono.
    """
    if rr.size < 40:
        return None
    y = np.cumsum(rr - np.mean(rr))
    ns = np.unique(np.round(np.logspace(np.log10(n_min), np.log10(min(n_max, rr.size // 4)), 12)).astype(int))
    F = []
    use = []
    for nbox in ns:
        if nbox < 4 or nbox > y.size // 2:
            continue
        nseg = y.size // nbox
        if nseg < 1:
            continue
        rms = []
        for i in range(nseg):
            seg = y[i * nbox:(i + 1) * nbox]
            x = np.arange(nbox)
            coef = np.polyfit(x, seg, 1)
            trend = np.polyval(coef, x)
            rms.append(np.mean((seg - trend) ** 2))
        F.append(np.sqrt(np.mean(rms)))
        use.append(nbox)
    if len(F) < 4 or not np.all(np.array(F) > 0):
        return None
    slope = np.polyfit(np.log(use), np.log(F), 1)[0]
    return float(slope)


def sample_entropy(rr: np.ndarray, m: int = 2, r_frac: float = 0.2) -> float | None:
    """SampEn — nepravilnost RR niza. Kratak snimak → orijentaciono."""
    x = np.asarray(rr, dtype=float)
    if x.size < 30:
        return None
    r = r_frac * float(np.std(x))
    if r <= 0:
        return None

    def phi(mm):
        N = x.size - mm
        if N < 5:
            return None
        cnt = []
        for i in range(N):
            win = x[i:i + mm]
            d = np.max(np.abs(np.array([x[j:j + mm] for j in range(N)]) - win), axis=1)
            cnt.append(np.sum(d <= r) - 1)
        c = np.array(cnt) / max(N - 1, 1)
        c = c[c > 0]
        return float(np.mean(-np.log(c))) if c.size else None

    a, b = phi(m + 1), phi(m)
    if a is None or b is None:
        return None
    return float(a - b)


def nonlinear_hrv(rr: np.ndarray, duration_s: float) -> dict:
    if rr.size < 30:
        return {}
    a1 = dfa_alpha1(rr)
    se = sample_entropy(rr)
    return {
        "dfa_alpha1": None if a1 is None else round(a1, 2),
        "sample_entropy": None if se is None else round(se, 3),
        "orientational": bool(duration_s < 300),
    }


# ================================================= 5. MAPA KVALITETA PO SEGMENTIMA
def noise_timeline(sig: np.ndarray, t: np.ndarray, fs: float, seg_s: float = 10.0) -> list[dict]:
    """Za svaki segment: udeo snage iznad 40 Hz (šum) i baseline wander."""
    x = np.asarray(sig, dtype=float)
    # Nominalni fs (npr. 192) ume da bude veći od izmerenog (npr. 190.6), pa krajnji
    # segment traži indeks VAN niza (dur*fs > broj uzoraka) → IndexError. Zato se
    # svaki indeks seče na stvarni broj uzoraka.
    n = int(min(x.size, len(t)))
    if n < int(2 * fs):
        return []
    dur = float(t[n - 1] - t[0])
    out = []
    for k in range(int(np.ceil(dur / seg_s))):
        a = min(int(k * seg_s * fs), n)
        b = min(int(min((k + 1) * seg_s, dur) * fs), n)
        if b - a < int(2 * fs):
            continue
        seg = x[a:b] - np.mean(x[a:b])
        f, pxx = sps.welch(seg, fs=fs, nperseg=min(512, seg.size))
        tot = float(np.trapezoid(pxx, f)) or 1e-12
        hf = f > 40
        bw = (f > 0.01) & (f < 0.5)
        out.append({
            "t0": round(float(t[a]), 1), "t1": round(float(t[b - 1]), 1),
            "hf_pct": round(float(np.trapezoid(pxx[hf], f[hf]) / tot * 100) if hf.sum() > 1 else 0.0, 1),
            "bw_pct": round(float(np.trapezoid(pxx[bw], f[bw]) / tot * 100) if bw.sum() > 1 else 0.0, 1),
        })
    return out


# ================================================= 6. RELATIVNI ST TREND
def st_trend(sig_wide: np.ndarray, rp: np.ndarray, t: np.ndarray, fs: float,
             seg_s: float = 15.0) -> dict:
    """Nivo ST (J+60 ms) po segmentima, izražen RELATIVNO (u % R amplitude).

    Bez kalibracije u mV i bez više odvoda ovo NIJE klinički ST nalaz — samo trend
    unutar snimka (npr. da li se nešto menja tokom vremena).
    """
    if rp.size < 8:
        return {}
    dur = float(t[-1] - t[0])
    mark = []
    for k in range(int(np.ceil(dur / seg_s))):
        t0, t1 = t[0] + k * seg_s, t[0] + (k + 1) * seg_s
        sel = rp[(t[rp] >= t0) & (t[rp] < t1)]
        mb = median_beat(sig_wide, None, sel, fs, 0.35, 0.40)
        if not mb:
            continue
        iv = intervals_from_median_beat(mb)
        if not iv:
            continue
        y, pre = mb["sig"], mb["pre"]
        qoff = iv["qrs_off_ms"]
        iso = iv.get("iso", 0.0)
        idx = pre + int((qoff + 60) / 1000 * fs)
        if not (0 <= idx < y.size):
            continue
        st = float(y[idx] - iso)
        r_amp = float(np.max(y) - np.min(y)) or 1e-9      # amplituda QRS od vrha do dna
        mark.append({"t0": round(t0 - t[0], 1), "st": st, "st_pct_r": st / r_amp * 100})
    if not mark:
        return {}
    # VAŽNO: sažetak (range_pct_r) se vraća UVEK kada ima i jedan segment — report,
    # index.php i plotter ga čitaju bez provere; ranije je kratak snimak (<3 segmenta)
    # vraćao samo {"points": mark} i izveštaj je pucao na KeyError 'range_pct_r'.
    ys = np.array([m["st_pct_r"] for m in mark], dtype=float)
    out = {
        "points": mark,
        "range_pct_r": [round(float(np.min(ys)), 1), round(float(np.max(ys)), 1)],
        "n_segments": len(mark),
        # trend ima smisla tek od 3 segmenta (2 tačke daju liniju kroz šum)
        "trend_ok": len(mark) >= 3,
    }
    if len(mark) >= 2:
        xs = np.array([m["t0"] for m in mark], dtype=float)
        slope = float(np.polyfit(xs, ys, 1)[0])
        out["slope_pct_per_min"] = round(slope * 60, 2)
        out["significant"] = bool(out["trend_ok"] and abs(slope * 60) > 5.0)
    return out


# ================================================= 7. MEDICINSKO TUMAČENJE
CANNOT = [
    "električnu osu srca (potrebna su najmanje 2 odvoda)",
    "hipertrofiju komora/pretkomora (potrebni odvodi V1/V5/V6 i kalibracija u mV)",
    "infarkt ili ožiljak (Q zupci, R progresija — više odvoda + kalibracija)",
    "ST elevaciju/depresiju u mV i ishemiju (kalibracija + više odvoda)",
    "tip bloka grane (LBBB vs RBBB zahteva V1 i V6)",
    "poreklo i tip aritmije (SVT/VT/ektopije po tipu)",
    "WPW/pre-ekscitaciju (PR + delta talas u više odvoda)",
    "QT kao osnov za odluku o lekovima (potreban čist, kalibrisan 12-kanalni)",
]


def _cls(v, lo, hi, short, normal, long, unit="ms"):
    if v is None or not np.isfinite(v):
        return None
    # Blizu granice se prikazuje decimala: na 192 Hz je grubost 5.2 ms, pa „120 ms — kratak"
    # za vrednost 119.8 ms izgleda kao greška u nalazu.
    nd = 1 if (min(abs(v - lo), abs(v - hi)) < 1.5) else 0
    if v < lo:
        return f"{v:.{nd}f} {unit} — {short} (ispod {lo})"
    if v > hi:
        return f"{v:.{nd}f} {unit} — {long} (iznad {hi})"
    return f"{v:.{nd}f} {unit} — {normal}"


def medical_interpretation(res: dict) -> dict:
    """Stručno tumačenje: šta je izmereno, šta odstupa, šta se ne može tvrditi."""
    hr = res.get("heart") or {}
    iv = res.get("intervals") or {}
    med = res.get("median_beat") or {}
    qual = res.get("quality") or {}
    hrv_t = (res.get("hrv") or {}).get("time") or {}
    extra = res.get("extra") or {}
    dur = float((res.get("sampling") or {}).get("duration_s") or 0)

    hr_med = float(hr.get("median") or 0)
    beats = int(hr.get("beats") or 0)
    hr_ok = (hr.get("cross_check_ok") is not False) and not hr.get("correction_reverted")
    rr_rej = float(qual.get("rr_artifact_pct") or 0)
    score = qual.get("score")

    # --- intervali: primarno sa medijalnog beata (otpornije od beat-po-beat)
    # Kada postoji konsenzus (particije udara / segmenti), on je primarni izvor: isti
    # medijalni beat se pravi više puta i vidi se da li se merenje menja kroz snimak.
    cons = res.get("consensus") or {}
    c_pr = cons.get("pr") or {}
    c_qrs = cons.get("qrs") or {}
    c_qt = cons.get("qt") or {}
    qrs_mb = med.get("qrs_ms")
    qrs_pb = (iv.get("qrs") or {}).get("median")
    qrs_pb = qrs_pb * 1000 if qrs_pb else None
    qrs_cons = c_qrs.get("median") if c_qrs.get("ok") else None
    qrs_used = qrs_cons or qrs_mb or qrs_pb
    qrs_src = ("konsenzus " + str(c_qrs.get("source"))) if qrs_cons else ("medijalni beat" if qrs_mb else "beat-po-beat")
    qrs_agree = (qrs_mb is not None and qrs_pb is not None and abs(qrs_mb - qrs_pb) <= 40)

    p = med.get("p") or {}
    pr_mb = p.get("pr_ms")
    pr_pb = (iv.get("pr") or {}).get("median")
    pr_pb = pr_pb * 1000 if pr_pb else None
    p_meas = int((iv.get("pr") or {}).get("n") or 0)
    p_cov = (p_meas / beats * 100) if beats else 0.0
    p_snr_v = float(p.get("snr") or 0)
    # PR se meri na MEDIJALNOM beatu: jasan P u usrednjenom beatu je dovoljan za
    # interval, i kada pojedinačna (beat-po-beat) detekcija P slabo prolazi.
    p_good = bool(p.get("present")) and p_snr_v >= 4.0
    p_rhythm_ok = p_cov >= 50.0
    # Redosled: konsenzus (grupe se slažu) → pojedinačni medijalni beat uz SNR ≥ 4 → blokada.
    # Konsenzus ne otkriva sistemsku grešku baze, ali otkriva nestabilno merenje; zato se
    # traži OBA: ispravna baza (vidi intervals_from_median_beat) i saglasnost grupa.
    pr_cons = c_pr.get("median") if c_pr.get("ok") else None
    pr_used = pr_cons or (pr_mb if (p_good and pr_mb and np.isfinite(pr_mb)) else None)
    pr_src = ("konsenzus %d/%d (%s)" % (c_pr.get("n_ok", 0), c_pr.get("n_total", 0), c_pr.get("source"))
              if pr_cons else ("medijalni beat, P SNR %.1f" % p_snr_v))

    t = med.get("t") or {}
    qt_used = t.get("qt_ms") if (t.get("reliable") and t.get("qt_ms")) else None
    qt_cons = c_qt.get("median") if c_qt.get("ok") else None
    qt_src = ("konsenzus %d/%d (%s)" % (c_qt.get("n_ok", 0), c_qt.get("n_total", 0), c_qt.get("source"))
              if qt_cons else "medijalni beat")
    if qt_cons:
        qt_used = qt_cons
    rr_med_s = (float(hr.get("median_rr_ms") or 0) / 1000.0) or None
    if not rr_med_s and hr_med:
        rr_med_s = 60.0 / hr_med
    qtc = None
    if qt_used and rr_med_s:
        qtc = qt_used / (rr_med_s ** 0.5)

    lines, anomalies, checks = [], [], []

    # 1) frekvencija
    if hr_ok and hr_med:
        if hr_med < 50:
            rate = f"{hr_med:.0f}/min — bradikardija (ispod 50)"
            anomalies.append(f"Bradikardija {hr_med:.0f}/min (proveriti da nije artefakt: sportisti/naspavan u miru je moguće)")
        elif hr_med < 60:
            rate = f"{hr_med:.0f}/min — na donjoj granici (60); često normalno u miru"
        elif hr_med <= 100:
            rate = f"{hr_med:.0f}/min — normalna frekvencija u miru (60–100)"
        elif hr_med <= 150:
            rate = f"{hr_med:.0f}/min — tahikardija (iznad 100)"
            anomalies.append(f"Tahikardija {hr_med:.0f}/min — ako je snimak u miru, ponoviti posle odmora")
        else:
            rate = f"{hr_med:.0f}/min — izražena tahikardija"
            anomalies.append(f"Izražena tahikardija {hr_med:.0f}/min")
        lines.append(f"Frekvencija: {rate}")
        checks.append(f"Frekvencija: dva nezavisna metoda se slažu (razlika {hr.get('cross_check_diff_pct', 0)}%)")
    else:
        lines.append("Frekvencija: NEPOUZDANA (metode se ne slažu / korekcija poništena) — broj otkucaja se ne tumači")
        checks.append("Frekvencija: uslov NIJE ispunjen (brojanje udara vs periodičnost signala)")

    # 2) ritam i ektopije
    e = extra.get("ectopics") or {}
    rng = e.get("rr_range_ms")
    if e:
        pre_n, pau_n = e.get("premature", 0), e.get("pauses", 0)
        var_pct = (rng[1] - rng[0]) / e["median_rr_ms"] * 100 if rng and e.get("median_rr_ms") else 0
        rhythm = "pravilan" if var_pct < 15 else "blago nepravilan" if var_pct < 25 else "nepravilan"
        lines.append(f"Ritam: {rhythm} (RR {rng[0]:.0f}–{rng[1]:.0f} ms, medijana {e['median_rr_ms']:.0f} ms)")
        if pre_n or pau_n:
            lines.append(f"  Preuranjeni udari: {pre_n}; pauze (RR > 1.6× medijane): {pau_n}"
                         + (f" na {e.get('pause_times_s')} s" if e.get("pause_times_s") else ""))
            if pau_n:
                anomalies.append(f"{pau_n} pauza u ritmu (najduži RR {e['longest_ms']:.0f} ms)")
            if pre_n >= 3:
                anomalies.append(f"{pre_n} preuranjenih udara — po tipu se ne mogu odrediti jednim odvodom")
        else:
            lines.append("  Preuranjenih udara i pauza nema.")
    if p_rhythm_ok and pr_used:
        lines.append(f"Sinusni ritam: verovatan — P talas pre QRS-a nađen u {p_cov:.0f}% udara")
        checks.append(f"P talas (ritam): detektovan u {p_cov:.0f}% udara (uslov ≥50% ispunjen)")
    else:
        lines.append(f"P talas: automatski praćen u {p_cov:.0f}% udara → sinusni ritam se ne može potvrditi; "
                     "u usrednjenom beatu P talas "
                     + (f"postoji (SNR {p_snr_v:.1f}, PR {pr_used:.0f} ms)" if pr_used else "nije jasan"))
        checks.append(f"P talas (ritam): uslov NIJE ispunjen (nađen u {p_cov:.0f}% udara, potrebno ≥50%)")

    # 3) intervali
    if qrs_used:
        lines.append("Intervali (mere se na medijalnom beatu; opseg 0.5–75 Hz):")
        # QRS se ne klasifikuje: jednoodvodno merenje na 192 Hz je sistemski uže od globalnog
        # QRS-a (normalan opseg odraslih 74–114 ms), pa bi oznaka "uzak" bila tvrdnja o metodi, ne o srcu.
        if qrs_used > 120 + PR_TOL_MS:
            _qlab = (f"{qrs_used:.0f} ms — širok (iznad 120); tip bloka se ne određuje jednim odvodom "
                     "— potvrditi 12-kanalnim")
        elif qrs_used < 80:
            _qlab = (f"{qrs_used:.0f} ms — izmereno; ne tumači se kao uzak QRS (jednoodvodno merenje "
                     "je uže od globalnog QRS-a; vrednost služi za trend)")
        else:
            _qlab = (f"{qrs_used:.0f} ms — u granicama na ovom merenju (jedan odvod; globalni QRS je "
                     "po pravilu duži)")
        lines.append("  " + _qlab + " [QRS]" + (f" · {qrs_src}" if qrs_cons else ""))
        if qrs_used > 120:
            if qrs_agree:
                anomalies.append(f"QRS {qrs_used:.0f} ms — širok (oba merenja se slažu); moguć blok grane ili "
                                 "komorski ritam, ali se TIP ne može odrediti jednim odvodom")
            else:
                anomalies.append(f"QRS {qrs_used:.0f} ms na medijalnom beatu (beat-po-beat {qrs_pb:.0f} ms) — "
                                 "širina sporna, potreban 12-kanalni ECG")
        checks.append(f"QRS: {qrs_src} {qrs_used:.0f} ms vs beat-po-beat (dwt) "
                      f"{qrs_pb and round(qrs_pb)} ms — "
                      + ("slažu se" if qrs_agree
                         else "dwt meri šire (verovatno obuhvata i okolne talase); medijalni beat je pouzdaniji")
                      + (f"; konsenzus {c_qrs.get('n_ok')}/{c_qrs.get('n_total')} grupa, "
                         f"raspon {c_qrs.get('min_ms'):.0f}–{c_qrs.get('max_ms'):.0f} ms" if qrs_cons else ""))
    if qrs_mb and not qrs_agree and qrs_mb <= 120:
        lines.append(f"  (beat-po-beat metoda daje {qrs_pb:.0f} ms — verovatno obuhvata i okolne talase; "
                     f"merenje na medijalnom beatu {qrs_mb:.0f} ms je pouzdanije)")
    if pr_used:
        # granica se ne tumači strože od tačnosti merenja (5.2 ms po uzorku na 192 Hz)
        if min(abs(pr_used - 120.0), abs(pr_used - 200.0)) <= PR_TOL_MS:
            _plab = (f"{pr_used:.0f} ms — na granici normale (120–200); razlika je unutar "
                     f"tačnosti merenja (±{PR_TOL_MS:.0f} ms)")
        else:
            _plab = _cls(pr_used, *NORM["pr"], "kratak", "normalan", "produžen") or ""
        lines.append("  " + _plab + f" [PR] · {pr_src}")
        # razlika manja od grubosti uzorka (5.2 ms na 192 Hz) nije nalaz
        if pr_used < 120 - PR_TOL_MS:
            anomalies.append(f"PR {pr_used:.0f} ms — kratak interval")
        elif pr_used > 200 + PR_TOL_MS:
            anomalies.append(f"PR {pr_used:.0f} ms — AV blok I stepena (produžen)")
        if pr_cons:
            checks.append(f"PR: konsenzus {c_pr.get('n_ok')}/{c_pr.get('n_total')} grupa se slaže "
                          f"(raspon {c_pr.get('min_ms'):.0f}–{c_pr.get('max_ms'):.0f} ms, "
                          f"IQR {c_pr.get('iqr_ms'):.0f} ms) — uslov ispunjen")
        else:
            checks.append(f"PR: P talas SNR {p_snr_v:.1f} u medijalnom beatu — uslov ispunjen")
    else:
        # najvažniji deo dijagnostike: raspon po grupama pokazuje DA LI je merenje stabilno
        if c_pr.get("n_ok"):
            lines.append(f"  PR: ne tumači se — {c_pr.get('reason')}; merenja po grupama "
                         f"{c_pr.get('min_ms'):.0f}–{c_pr.get('max_ms'):.0f} ms "
                         f"({c_pr.get('n_ok')}/{c_pr.get('n_total')} grupa upotrebljivo)")
            checks.append(f"PR: grupe se ne slažu (IQR {c_pr.get('iqr_ms')} ms) → interval se ne tumači")
        else:
            lines.append(f"  PR: ne tumači se (P talas pouzdano nađen u {p_cov:.0f}% udara, SNR "
                         f"{p_snr_v:.1f}; potrebno ≥50% i SNR ≥4, ili saglasnost segmenata)")
            checks.append("PR: uslov NIJE ispunjen → interval se prikazuje samo kao merenje, bez zaključka")
    if qt_used:
        # QT se prikazuje kao IZMERENO, bez pragova: tangenta i beat-po-beat metoda se kod nas
        # razilaze >100 ms (u literaturi ~10 ms), pa QTc nije osnov za „produžen"/„u granicama".
        _qt_dwt = float((iv.get("qt") or {}).get("median") or 0) * 1000.0
        _delta = abs(float(qt_used) - _qt_dwt) if _qt_dwt else 0.0
        lines.append(f"  QT {qt_used:.0f} ms — izmereno; QT/QTc se NE tumači"
                     + (f" (metode se razilaze {_delta:.0f} ms)" if _delta > 40
                        else " (merenje nije validirano na ovom hardveru)")
                     + (f" · {qt_src}" if qt_cons else ""))
        checks.append(f"T talas: SNR {t.get('snr', 0):.1f} — {'dovoljan' if t.get('snr', 0) > 3 else 'nedovoljan'}"
                      + (f"; QT konsenzus {c_qt.get('n_ok')}/{c_qt.get('n_total')} grupa, "
                         f"raspon {c_qt.get('min_ms'):.0f}–{c_qt.get('max_ms'):.0f} ms" if qt_cons else "")
                      + "; QT/QTc bez zaključka (nije validirano)")
    else:
        lines.append("  QT/QTc: ne tumači se (T talas nije pouzdano izmeren)" + (
            f" — grupe se ne slažu ({c_qt.get('reason')})" if c_qt.get("n_ok") else ""))

    # 4) HRV
    if hrv_t.get("rmssd_ms"):
        note = " (orijentaciono — snimak kraći od 5 min)" if dur < 300 else ""
        lines.append(f"HRV: SDNN {hrv_t.get('sdnn_ms', 0):.0f} ms, RMSSD {hrv_t.get('rmssd_ms', 0):.0f} ms, "
                     f"pNN50 {hrv_t.get('pnn50_pct', 0):.1f}%{note}")
        nl = extra.get("nonlinear") or {}
        if nl.get("dfa_alpha1") is not None:
            lines.append(f"  DFA α1 {nl['dfa_alpha1']} (0.5–1.5 uobičajeno; niže = manje složena varijabilnost), "
                         f"SampEn {nl.get('sample_entropy')}")
        if rr_rej > 5:
            lines.append(f"  (HRV uz {rr_rej:.0f}% odbačenih intervala nije pouzdan)")
        elif hrv_t.get("rmssd_ms", 99) < 20 and dur >= 60:
            lines.append("  Niska varijabilnost u miru — može biti stres, umor, bolest, kafa, ali i sama posledica "
                         "kratkog snimka")
    if extra.get("respiration"):
        r = extra["respiration"]
        lines.append(f"Disanje (iz sporih oscilacija signala): {r['breaths_per_min']}/min "
                     f"(dominacija {r['dominance_pct']:.0f}% — "
                     f"{'jasno izraženo' if r['dominance_pct'] > 30 else 'slabo izraženo'})")
    st = extra.get("st_trend") or {}
    if st.get("range_pct_r"):
        rng = st["range_pct_r"]
        trend = (f"trend {st.get('slope_pct_per_min', 0):+.1f} %R/min"
                 if st.get("trend_ok")
                 else f"trend se ne ocenjuje (samo {st.get('n_segments', 0)} segment(a) — snimak je prekratak)")
        lines.append(f"ST nivo (relativno, J+60 ms): {rng[0]}…{rng[1]} % R amplitude, " + trend
                     + ("  ← vredno pažnje" if st.get("significant") else ""))
        lines.append("  (bez kalibracije u mV ovo je samo relativna promena unutar snimka, ne klinički ST nalaz)")

    verdict_bits = []
    verdict_bits.append("snimak upotrebljiv za frekvenciju i ritam" if hr_ok else "frekvencija nepouzdana")
    if pr_used:
        verdict_bits.append("P talas izmeren → PR se tumači; QRS je samo izmeren (jednoodvodno)")
    else:
        verdict_bits.append("fini intervali (PR) se ne mogu čitati")
    if not anomalies:
        verdict_bits.append("nema izmerenih odstupanja")
    verdict = "; ".join(verdict_bits)

    next_steps = []
    if not hr_ok or rr_rej > 10:
        next_steps.append("Ponoviti snimanje u miru, sa čistim i čvrstim kontaktom elektroda (10–30 s je dovoljno za ritam).")
    if (qrs_used or 0) > 120:
        next_steps.append("Za širinu QRS i eventualni blok grane potreban je standardni 12-kanalni ECG (jednim odvodom se ne može).")
    if pr_used and (pr_used < 120 - PR_TOL_MS or pr_used > 200 + PR_TOL_MS):
        next_steps.append("Za PR interval i eventualni pre-eksitacioni obrazac potreban je 12-kanalni ECG.")
    if dur < 300:
        next_steps.append("Za standardnu HRV (Task Force 1996) snimiti 5 min u miru; sadašnje vrednosti su orijentacione.")
    if score is not None and score < 70:
        next_steps.append("Popraviti kvalitet signala (elektrode, kabl, bez telefona/punjača u blizini) pa ponoviti.")
    next_steps.append("Ako ima tegoba (lupanje, vrtoglavica, gušenje, bol u grudima) — snimak pokazati lekaru; "
                      "ovaj nalaz nije dijagnoza.")

    return {
        "lines": lines,
        "checks": checks,
        "anomalies": anomalies,
        "cannot": CANNOT,
        "verdict": verdict,
        "next_steps": next_steps,
        "pr_used_ms": None if pr_used is None else round(float(pr_used), 0),
        "pr_source": pr_src if pr_used else None,
        "pr_consensus_ok": bool(pr_cons),
        "pr_consensus": ({k: c_pr.get(k) for k in ("median", "iqr_ms", "min_ms", "max_ms",
                                                   "n_ok", "n_total", "ok", "reason", "source")}
                         if c_pr else None),
        "qrs_used_ms": None if not qrs_used else round(float(qrs_used), 0),
        "qtc_ms": None if not qtc else round(float(qtc), 0),
        "p_coverage_pct": round(p_cov, 1),
    }
