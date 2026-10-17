#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ECG analiza — jedan ulaz (CSV), jedan izlaz (report.txt + results.json + PNG).

Zamenjuje stari par ecg_details.py / ecg_intervals.py (koji je signal obrađivao
5x i vraćao nan za PR interval).

Ključne razlike u odnosu na stari kod:
  * vremenska osa se gradi iz Timestamp kolone (ako postoji) → svi intervali u
    sekundama se računaju iz stvarnog vremena, ne iz (pogrešno pretpostavljene)
    fiksne frekvencije;
  * NaN iz neurokitove delineacije se filtrira (stari kod je zbog toga vraćao
    "Mean: nan" za PR interval iako je prijavljivao 125 merenja);
  * P talas se uparuje sa QRS-om po vremenu (najbliži prethodni), a ne po
    indeksu — jedan promašen P talas više ne pomera sve ostale;
  * QRS trajanje iz QRS onset→offset (staro Q peak→S peak je podcenjivalo);
  * QTc po Bazettu I Fridericiji, sa RR intervalom koji stvarno prethodi beatu;
  * procena kvaliteta signala zasnovana na merljivim veličinama (saturacija,
    50 Hz, HF šum, artefakti) umesto izmišljenog "SNR = std(x)/std(diff(x))".

Primer:
  /opt/ecg-venv/bin/python3 ecg_analyze.py --input data/data.csv \
      --outdir data --fs 192 --job-json data/job.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# dodatne analize + medicinsko tumačenje (opciono: ako modul nedostaje, analiza radi dalje)
try:
    from ecg_medical import (median_beat, intervals_from_median_beat, ectopics_and_pauses,
                             respiration_rate, nonlinear_hrv, noise_timeline, st_trend,
                             medical_interpretation)
    HAS_MEDICAL = True
except Exception:  # pragma: no cover
    HAS_MEDICAL = False

# konsenzus merenja preko particija udara i segmenata snimka (zaseban modul, isti tretman)
try:
    import ecg_segments
    HAS_SEGMENTS = HAS_MEDICAL
except Exception:  # pragma: no cover
    HAS_SEGMENTS = False

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

import neurokit2 as nk
from scipy import signal as sps

# ---------------------------------------------------------------- konstante --
SERIES_COLOR = "#1f4e9c"
OLD_PREFIX_KEYS = ("S1",)

RANGE_PR = (0.12, 0.20)      # s, normalno
RANGE_QRS_MAX = 0.12         # s
RANGE_QTC_MAX = 0.45         # s (Bazett, konzervativno)
RR_MIN, RR_MAX = 0.30, 2.00  # s, fiziološki opseg
# Autokorelacioni period je referenca za uklanjanje T talasa samo iznad ovoga:
# sa peak_corr 0.17 je referenca bila 2x pogresna i pojedeno je 60% pravih udara.
AC_MIN_PEAK_CORR = 0.30
# T talas je znatno manji od R. Ako uklonjeni vrhovi imaju slicnu amplitudu
# zadrzanima, uklonjeni su pravi R talasi -> korekciju vratiti.
AMP_REMOVED_KEPT_RATIO = 0.60
# Pod za MAD u RR filteru (u log prostoru = 2 %): bez njega na vrlo pravilnom snimku
# MAD padne na ~1 ms pa se obicna varijacija od par ms broji kao artefakt.
RR_MAD_FLOOR = 0.02
DISCLAIMER = (
    "Ovaj izveštaj je informativan i NIJE medicinska dijagnoza. Signal je snimljen "
    "jednokanalnim ADC uređajem bez kalibracije u mV, pa se amplitude ne tumače "
    "klinički. Za svaku odluku o zdravlju konsultovati lekara."
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ ulaz -----
def _sniff(path: str):
    """Vrati (ima_header, separator) na osnovu prvih redova fajla."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = []
        for _ in range(50):
            line = fh.readline()
            if not line:
                break
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
        if not lines:
            raise ValueError("CSV fajl je prazan")
        head = lines[0]

    has_header = re.match(r"^\s*[-+]?[0-9.]", head) is None
    sep = None
    for cand in (",", ";", "\t", " "):
        if cand in head:
            sep = cand
            break
    return has_header, (sep or ",")


def load_signal(path: str, fs_nominal: float | None):
    """Učitaj CSV → (signal, vremenska osa [s] ili None, meta)."""
    has_header, sep = _sniff(path)
    df = pd.read_csv(
        path,
        sep=sep,
        engine="python",
        comment="#",
        header=0 if has_header else None,
        encoding="utf-8-sig",
        skip_blank_lines=True,
        on_bad_lines="skip",
    )
    if df.shape[1] == 1 and sep in (",", ";", "\t", " ") and not has_header:
        # jedan broj po redu → jedan kanal
        df.columns = ["S1"]
    df = df.dropna(axis=1, how="all")
    if df.empty or df.shape[1] == 0:
        raise ValueError("CSV nema upotrebljivih kolona")

    # --- vremenska kolona
    ts_col = None
    for c in df.columns:
        name = str(c).strip().lower()
        if name in ("timestamp", "time", "t", "datetime", "date") or "time" in name or name in ("vreme",):
            ts_col = c
            break

    # --- numeričke kolone
    numeric: dict[object, pd.Series] = {}
    for c in df.columns:
        if c == ts_col:
            continue
        numeric[c] = pd.to_numeric(df[c].astype(str).str.strip(), errors="coerce")
    if not numeric:
        raise ValueError("CSV nema numeričkih kolona")

    # --- izbor signalne kolone
    best_col, best_key = None, -1
    for c, s in numeric.items():
        name = str(c).strip().lower()
        valid = int(s.notna().sum())
        pref = 3 if name in ("s1", *[k.lower() for k in OLD_PREFIX_KEYS]) else 0
        pref += 2 if name in ("ecg", "ekg", "signal", "adc", "value", "val", "amplitude") else 0
        pref += 1 if name.startswith(("ch", "channel", "a")) else 0
        key = valid + pref * 1_000_000
        if valid > 0 and key > best_key:
            best_col, best_key = c, key
    signal = numeric[best_col].dropna().astype(float).values

    # --- vreme
    times = None
    ts_note = "bez Timestamp kolone → vreme = indeks / fs"
    if ts_col is not None:
        raw = df[ts_col].astype(str).str.strip()
        if np.issubdtype(df[ts_col].dtype, np.number):
            t = pd.to_numeric(df[ts_col], errors="coerce").to_numpy(dtype=float)
            ts_note = "numerička vremenska kolona [s]"
        else:
            t = pd.to_datetime(raw, errors="coerce", utc=True)
            ts_note = "ISO timestamp kolona"
            t = t.astype("int64").to_numpy(dtype=float) / 1e9  # s
        ok = np.isfinite(t)
        if ok.mean() > 0.9:
            idx = numeric[best_col].notna().to_numpy()
            t = t[idx | ~ok]  # poravnaj sa signalom
            t = t[: len(signal)]
            if len(t) == len(signal) and np.all(np.isfinite(t)):
                times = t
                ts_note += f" ({len(np.unique(t))} različitih vremena)"
            else:
                ts_note = "Timestamp kolona ne može da se poravna → koristi se indeks"
                times = None

    meta = {
        "rows": int(df.shape[0]),
        "signal_column": str(best_col),
        "timestamp_note": ts_note,
    }
    return signal, times, meta


def build_time_axis(times: np.ndarray | None, n: int, fs: float):
    """
    Vremenska osa u sekundama od početka snimanja.

    Uređaj šalje uzorke u paketima, a Web Serial ih isporučuje u chunk-ovima —
    zato više uzoraka deli isti (ms) timestamp. Uzorci unutar istog chunk-a se
    raspoređuju linearno između prethodnog i trenutnog vremena, čime osa postaje
    monotona i ujednačena. (Bez toga bi 5-8 uzoraka imalo isto vreme → fs→∞.)
    """
    if times is None or len(times) != n:
        return np.arange(n) / fs, None

    t = np.asarray(times, dtype=float)
    t = t - t[0]
    out = t.copy()
    prev = None
    i = 0
    while i < n:
        j = i
        while j + 1 < n and t[j + 1] == t[i]:
            j += 1
        count = j - i + 1
        if count > 1 and prev is not None and t[i] > prev:
            step = (t[i] - prev) / count
            for k in range(count):
                out[i + k] = prev + step * (k + 1)
        prev = t[i]
        i = j + 1
    span = out[-1] - out[0]
    if span <= 0:
        return np.arange(n) / fs, None
    return out, (n - 1) / span


# -------------------------------------------------------------- metrike -----
def parse_markers(path: str):
    """Intervali koje je aplikacija odbacila (sekunde od pocetka snimka).

    `# PAUSE`/`# RESUME` = delovi u kojima je aplikacija prestala da upisuje uzorke
    (sum/pokret, saturacija, otpala elektroda), `# GAP` = prekid toka podataka.
    NI JEDNO od toga NIJE srcana pauza — bez ovoga RR preko rupe izlazi kao
    "pauza u ritmu" (na snimku od 29.5 min: 125 lazinh pauza i RR 36 s).
    """
    pauses, gaps, cur = [], [], None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("#"):
                    continue
                m = re.search(r"t=(\d+):(\d+)", line)
                sec = (int(m.group(1)) * 60 + int(m.group(2))) if m else None
                if line.startswith("# PAUSE") and sec is not None:
                    cur = (sec, line.split("label=")[-1].strip())
                elif line.startswith("# RESUME") and cur is not None and sec is not None:
                    pauses.append((cur[0], sec, cur[1]))
                    cur = None
                elif line.startswith("# GAP") and sec is not None:
                    g = re.search(r"s=([\d.]+)", line)
                    gaps.append((sec, float(g.group(1)) if g else 0.0))
    except OSError:
        return [], []
    if cur is not None:  # pauza bez RESUME (snimak prekinut u pauzi)
        pauses.append((cur[0], None, cur[1]))
    return pauses, gaps


def dropped_intervals(pauses, gaps, t_end: float):
    """Spojeni intervali [t0, t1] odbacenih delova, u granicama snimka."""
    iv = [(float(a), float(t_end if b is None else b)) for a, b, _l in pauses]
    iv += [(float(g0), float(g0 + gl)) for g0, gl in gaps]
    out = []
    for a, b in sorted(iv):
        a, b = max(0.0, a), max(0.0, b)
        if b <= a:
            continue
        if out and a <= out[-1][1] + 0.05:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _in_intervals(x: float, iv) -> bool:
    return any(a <= x < b for a, b in iv)


def _span_crosses(a: float, b: float, iv) -> bool:
    """Da li RR interval [a, b] dodiruje odbaceni deo (tada nije merljiv)."""
    return any(not (b <= s or a >= e) for s, e in iv)


def _stat(values, robust: bool = True) -> dict:
    """Medijana + IQR; uz blagi MAD filter koji odbacuje očigledne greške merenja.

    Intervali (PR/QRS/QT) su MERENJA, ne fiziološka varijabilnost — pojedinačni
    beat sa loše postavljenim onset-om daje npr. QRS od 190 ms i kvari prosek.
    Zato se uz medijanu (koja je robustna sama po sebi) prijavljuje i koliko je
    merenja odbačeno, da se vidi kvalitet.
    """
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    n_all = int(v.size)
    rejected = 0
    if robust and n_all >= 8:
        med = np.median(v)
        mad = np.median(np.abs(v - med))
        if mad > 0:
            keep = np.abs(v - med) <= 3.5 * 1.4826 * mad
            if keep.sum() >= max(5, 0.5 * n_all):
                rejected = int((~keep).sum())
                v = v[keep]
    if v.size == 0:
        return {"n": 0, "n_meas": n_all}
    q1, q3 = np.percentile(v, [25, 75])
    return {
        "n": int(v.size),
        "n_meas": n_all,
        "n_rejected": rejected,
        "mean": float(np.mean(v)),
        "median": float(np.median(v)),
        "std": float(np.std(v, ddof=1)) if v.size > 1 else 0.0,
        "iqr": [float(q1), float(q3)],
        "p5": float(np.percentile(v, 5)),
        "p95": float(np.percentile(v, 95)),
        "min": float(np.min(v)),
        "max": float(np.max(v)),
    }


def correct_double_counts(rp: np.ndarray, sig_clean: np.ndarray, expected_rr: float | None,
                          trusted: bool = True):
    """Ukloni lažne udare (klasično: T talas se broji kao R) pomoću dve provere:
    (1) RR interval znatno kraći od očekivanog (iz periodičnosti signala),
    (2) od dva bliska vrha zadrži onaj sa većom amplitudom.

    `expected_rr` je u UZORCIMA (sekunde * fs). Neophodno jer neurokit na signalu
    sa velikim T talasom i šumom ume da nađe ~2x više udara nego što signal ima
    (izveštaj onda lažno kaže tahikardiju). Bez pouzdanog `expected_rr` ne diramo ništa.
    """
    if expected_rr is None or len(rp) < 8 or not trusted:
        return rp, 0, False
    rp = np.asarray(sorted(int(p) for p in rp))
    rp_orig = rp.copy()
    n0 = len(rp)
    amp_orig = np.abs(sig_clean[rp_orig])
    amp = amp_orig.copy()
    removed = 0
    for _ in range(6):
        changed = False
        keep = [True] * len(rp)
        prev = None
        for i in range(len(rp)):
            if prev is None:
                prev = i
                continue
            if (rp[i] - rp[prev]) < 0.62 * expected_rr:
                # dva preblizu vrha → manje izražen je verovatno T talas
                weaker = i if amp[i] < amp[prev] else prev
                keep[weaker] = False
                removed += 1
                changed = True
                if weaker == prev:
                    prev = i
            else:
                prev = i
        if not changed:
            break
        idx = np.where(keep)[0]
        rp, amp = rp[idx], amp[idx]
    # Ako je "korekcija" pojela većinu udara, referenca je bila pogrešna (npr.
    # autokorelacija uhvatila period disanja) → vraćamo original i javljamo to.
    if len(rp) < 0.35 * n0:
        return rp_orig, 0, True
    if removed and len(rp) < 0.45 * n0:
        return rp_orig, 0, True
    # T talas je znatno manji od R talasa. Uklonjeni vrhovi slicne amplitude su
    # PRAVI R talasi (oktavna greska reference: 16.10.2026. odnos 0.82 vs 0.42 kod
    # ispravnog uklanjanja T talasa) → vrati original.
    if removed:
        kept = np.isin(rp_orig, rp)
        if kept.any() and (~kept).any():
            med_kept = float(np.median(amp_orig[kept]))
            med_rm = float(np.median(amp_orig[~kept]))
            if med_kept > 0 and (med_rm / med_kept) > AMP_REMOVED_KEPT_RATIO:
                return rp_orig, 0, True
    return rp, removed, False


def detect_beats(sig_clean: np.ndarray, fs: float):
    _, info = nk.ecg_peaks(sig_clean, sampling_rate=fs, method="neurokit", correct_artifacts=False)
    rp = np.asarray(info.get("ECG_R_Peaks", []), dtype=int)
    if len(rp) < 3:
        raise ValueError(f"Detektovano je samo {len(rp)} R talasa — snimak je prekratak ili previše šumovit")
    return rp


def hr_autocorr(sig_clean: np.ndarray, fs: float, lo_bpm: float = 30.0, hi_bpm: float = 210.0):
    """Nezavisna procena HR iz periodičnosti signala (autokorelacija preko FFT).

    Služi kao KONTROLA detekcije udara: brojanje R talasa ume da pogreši na
    šumovitom signalu ili uz veliki T talas (broji T kao udar → lažna tahikardija).
    Ako se brojanje i periodičnost ne slažu, tumačenje ritma/morfologije se blokira.
    """
    x = np.asarray(sig_clean, dtype=float)
    x = x - np.mean(x)
    n = len(x)
    lag_min = max(1, int(round(fs * 60.0 / hi_bpm)))
    lag_max = min(int(round(fs * 60.0 / lo_bpm)), n // 2)
    if n < 4 * lag_min or lag_max <= lag_min + 2:
        return None
    nfft = 1 << int(np.ceil(np.log2(max(2 * n, 2 * lag_max + 2))))
    f = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(f * np.conj(f), nfft)[: lag_max + 1]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    seg = ac[lag_min : lag_max + 1]
    peak = int(np.argmax(seg)) + lag_min
    # Ako je vrh na dvostrukom/trostrukom periodu (klasično kad je T talas velik
    # ili kad postoji spora oscilacija disanja), uzmi osnovni period — inače bi
    # HR ispao upola manji nego što stvarno jeste.
    for div in (2, 3):
        cand = int(round(peak / div))
        if cand < lag_min:
            break
        w = max(2, cand // 10)
        lo, hi = max(lag_min, cand - w), min(lag_max, cand + w)
        if hi <= lo:
            continue
        sub = lo + int(np.argmax(ac[lo : hi + 1]))
        if ac[sub] >= 0.6 * ac[peak]:
            peak = sub
            break
    delta = 0.0
    if 0 < peak < len(ac) - 1:
        y0, y1, y2 = ac[peak - 1], ac[peak], ac[peak + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-12:
            delta = float(np.clip(0.5 * (y0 - y2) / den, -1.0, 1.0))
    period_s = (peak + delta) / fs
    corr = float(ac[peak])
    return {"hr_bpm": 60.0 / period_s, "period_s": period_s, "peak_corr": corr,
            # 0.25 = periodičnost postoji ali je slaba; ispod toga je nema smisla koristiti
            "ok": bool(corr >= 0.25)}


def independent_hr(sig_raw: np.ndarray, t: np.ndarray, fs: float):
    """Nezavisna procena HR DRUGIM algoritmom (band-pass 5-25 Hz + anvelopa + vrhovi).

    Sluzi kao referenca za kontrolu korekcije i kao zamena za autokorelaciju kada je ona
    nepouzdana. Ne zavisi ni od neurokit detekcije ni od autokorelacije.
    """
    try:
        x = np.asarray(sig_raw, dtype=float)
        lo = 5.0 / (fs / 2.0)
        hi = min(25.0, fs / 2.0 - 1.0) / (fs / 2.0)
        b, a = sps.butter(3, [lo, hi], btype="band")
        env = np.abs(sps.filtfilt(b, a, x))
        w = max(1, int(0.05 * fs))
        env = np.convolve(env, np.ones(w) / w, mode="same")
        pk, _ = sps.find_peaks(env, distance=int(0.35 * fs),
                               height=0.5 * np.percentile(env, 99))
    except Exception:
        return None
    if pk.size < 6 or t is None or len(t) != x.size:
        return None
    rr = np.diff(t[pk])
    rr = rr[(rr > 0.25) & (rr < 3.0)]
    if rr.size < 5:
        return None
    return {"hr_bpm": float(60.0 / np.median(rr)), "n": int(pk.size),
            "rr_ms": float(np.median(rr) * 1000)}


def rr_analysis(rp: np.ndarray, t: np.ndarray):
    """RR intervali + fiziološki/artefakt filteri (bez menjanja samih R talasa)."""
    rr = np.diff(t[rp])
    phys = (rr >= RR_MIN) & (rr <= RR_MAX)
    # robustni outlier test na log(RR): 3 x MAD
    clean = phys.copy()
    if phys.sum() >= 8:
        lr = np.log(rr[phys])
        med = np.median(lr)
        mad = np.median(np.abs(lr - med)) or 1e-6
        # Pod za MAD: na vrlo pravilnom snimku (CV < 1 %) MAD je ~1 ms, pa bi filter
        # odbacio normalnu varijaciju od par ms i prijavio 40 % "artefakata" (16.10.2026,
        # sinteticki snimak: HR tacan, a kvalitet 61 zbog toga). Pravi artefakti
        # (ektopije, udvojeni udari) su 20 %+ od medijane i daleko iznad poda.
        mad = max(mad, RR_MAD_FLOOR)
        z = np.abs(lr - med) / (1.4826 * mad)
        clean[phys] = z <= 3.5
    return {
        "rr_all": rr,
        "mask_phys": phys,
        "mask_clean": clean,
        "n_phys_out": int((~phys).sum()),
        "n_outlier": int((phys & ~clean).sum()),
    }


def hrv_time(rr: np.ndarray) -> dict:
    if rr.size < 3:
        return {}
    drr = np.diff(rr)
    return {
        "mean_rr_ms": float(np.mean(rr) * 1000),
        "sdnn_ms": float(np.std(rr, ddof=1) * 1000),
        "rmssd_ms": float(np.sqrt(np.mean(drr ** 2)) * 1000),
        "sdsd_ms": float(np.std(drr, ddof=1) * 1000),
        "pnn50_pct": float(np.mean(np.abs(drr) > 0.05) * 100),
        "sd1_ms": float(np.std(drr, ddof=1) / np.sqrt(2) * 1000),
        "sd2_ms": float(np.sqrt(2 * np.var(rr, ddof=1) - 0.5 * np.var(drr, ddof=1)) * 1000),
        "cv_pct": float(np.std(rr, ddof=1) / np.mean(rr) * 100),
    }


def hrv_freq(rr: np.ndarray, rr_times: np.ndarray) -> dict:
    """Welch na interpoliranom RR tachogramu (Task Force opsezi).

    rr[i] je interval koji se ZAVRŠAVA u trenutku rr_times[i] (oba niza su
    već filtrirana i poravnata).
    """
    if rr.size < 10 or rr.size != rr_times.size:
        return {}
    duration = float(rr_times[-1] - rr_times[0])
    if duration < 30:
        return {"note": f"snimak {duration:.0f}s — prekratak za spektralnu analizu"}
    fs_i = 4.0
    t_i = np.arange(rr_times[0], rr_times[-1], 1 / fs_i)
    if t_i.size < 32:
        return {}
    x = np.interp(t_i, rr_times, rr)
    x = x - np.mean(x)
    f, pxx = sps.welch(x, fs=fs_i, nperseg=min(256, x.size), detrend=False)
    bands = {"vlf": (0.0033, 0.04), "lf": (0.04, 0.15), "hf": (0.15, 0.40)}
    out = {}
    for name, (lo, hi) in bands.items():
        m = (f >= lo) & (f < hi)
        out[f"{name}_ms2"] = float(np.trapezoid(pxx[m], f[m]) * 1e6) if m.sum() > 1 else 0.0
    # VLF iz kratkog zapisa se ne tumači (Task Force 1996: fiziološko značenje nedefinisano
    # ispod ~5 min; za VLF/ULF je potreban 24-h zapis). Ostaje None da se ne bi citirao kao nalaz.
    if duration < 600.0:
        out["vlf_ms2"] = None
        out["vlf_note"] = "VLF se ne tumači iz zapisa kraćeg od 10 min (Task Force 1996)"
    out["total_ms2"] = float(np.trapezoid(pxx, f) * 1e6)
    if out["hf_ms2"] > 0:
        out["lf_hf"] = float(out["lf_ms2"] / out["hf_ms2"])
        out["lf_nu"] = float(out["lf_ms2"] / (out["lf_ms2"] + out["hf_ms2"]) * 100)
        out["hf_nu"] = float(out["hf_ms2"] / (out["lf_ms2"] + out["hf_ms2"]) * 100)
    out["duration_s"] = duration
    if duration < 300:
        out["note"] = (
            f"snimak {duration:.0f}s — LF/HF se po standardu (Task Force 1996) tumači "
            "tek od 5 min, kraće je samo orijentaciono"
        )
    return out


def delineate(sig_clean: np.ndarray, rp: np.ndarray, fs: float):
    """Probaj 'peak' i 'dwt', izaberi metodu sa više validnih tačaka."""
    results, scores = {}, {}
    for method in ("dwt", "peak"):
        try:
            _, waves = nk.ecg_delineate(sig_clean, rp, sampling_rate=fs, method=method, show=False)
            waves = {k: np.asarray(v, dtype=float) for k, v in waves.items() if k.startswith("ECG_")}
            finite = lambda k: int(np.isfinite(waves.get(k, np.array([]))).sum())
            score = (
                finite("ECG_R_Onsets") + finite("ECG_R_Offsets") + finite("ECG_T_Offsets")
                + 0.5 * finite("ECG_P_Onsets") + 0.5 * finite("ECG_P_Peaks")
            )
            results[method] = waves
            scores[method] = score
        except Exception as exc:  # pragma: no cover
            log(f"  ! delineacija metodom {method} nije uspela: {exc}")
    if not results:
        raise ValueError("Delineacija talasa nije uspela ni jednom metodom")
    best = max(scores, key=lambda m: (scores[m], m == "dwt"))
    log(f"  Delineacija: metod '{best}' (skor {scores[best]:.1f}; peak={scores.get('peak', 0):.1f})")
    return best, results[best], scores


def _nearest_preceding(sorted_vals: np.ndarray, pos: float, lo: float, hi: float):
    """Najveća vrednost iz sorted_vals koja je u opsegu [pos-hi, pos-lo]."""
    if sorted_vals.size == 0:
        return None
    k = int(np.searchsorted(sorted_vals, pos, side="left")) - 1
    if k < 0:
        return None
    val = float(sorted_vals[k])
    if pos - val < lo or pos - val > hi:
        return None
    return val


def intervals_for_beats(waves: dict, rp: np.ndarray, t: np.ndarray, fs: float):
    """Merenja po beatu; sve dužine u sekundama iz vremenske ose.

    P talas se traži po VREMENU (najbliži prethodni P unutar 60–450 ms pre QRS
    onset-a), a ne po indeksu u nizu — inače jedan promašen P pomera sve ostale.
    """
    n = len(rp)
    w_lo, w_hi = int(round(0.06 * fs)), int(round(0.45 * fs))  # uzorci

    def arr(key):
        v = np.asarray(waves.get(key, np.full(n, np.nan)), dtype=float)
        if v.size < n:
            v = np.concatenate([v, np.full(n - v.size, np.nan)])
        return v[:n]

    r_on, r_off = arr("ECG_R_Onsets"), arr("ECG_R_Offsets")
    p_on, p_pk = arr("ECG_P_Onsets"), arr("ECG_P_Peaks")
    t_off = arr("ECG_T_Offsets")
    q_pk, s_pk = arr("ECG_Q_Peaks"), arr("ECG_S_Peaks")

    p_on_vals = np.sort(p_on[np.isfinite(p_on)])
    p_pk_vals = np.sort(p_pk[np.isfinite(p_pk)])

    pr, pr_src = [], []
    qrs, qt, qtc_b, qtc_f = [], [], [], []
    for i in range(n):
        # --- QRS: onset → offset (fallback: Q peak → S peak)
        on, off = r_on[i], r_off[i]
        if not (np.isfinite(on) and np.isfinite(off)):
            if np.isfinite(q_pk[i]) and np.isfinite(s_pk[i]):
                on, off = q_pk[i], s_pk[i]
        if np.isfinite(on) and np.isfinite(off) and off > on:
            d = t[int(off)] - t[int(on)]
            if 0.04 <= d <= 0.20:
                qrs.append(d)

        # --- PR: QRS onset − P onset (P pre QRS-a, po vremenu)
        if np.isfinite(on):
            val = _nearest_preceding(p_on_vals, on, w_lo, w_hi)
            src = "meren"
            if val is None:
                val = _nearest_preceding(p_pk_vals, on, w_lo, w_hi)
                src = "procenjen"
            if val is not None:
                pr_val = t[int(on)] - t[int(val)]
                if 0.06 <= pr_val <= 0.40:
                    pr.append(pr_val)
                    pr_src.append(src)

        # --- QT: QRS onset → T offset; QTc sa RR koja stvarno prethodi beatu
        if np.isfinite(on) and np.isfinite(t_off[i]) and t_off[i] > on:
            qt_val = t[int(t_off[i])] - t[int(on)]
            if 0.20 <= qt_val <= 0.70:
                qt.append(qt_val)
                if i > 0:
                    rr_prev = t[rp[i]] - t[rp[i - 1]]
                    if rr_prev > 0.2:
                        qtc_b.append(qt_val / np.sqrt(rr_prev))
                        qtc_f.append(qt_val / rr_prev ** (1 / 3))
    return {
        "pr": pr,
        "pr_sources": pr_src,
        "qrs": qrs,
        "qt": qt,
        "qtc_bazett": qtc_b,
        "qtc_fridericia": qtc_f,
    }


def signal_quality(sig: np.ndarray, sig_clean: np.ndarray, fs: float, rr_info: dict, waves: dict, rp: np.ndarray,
                   dropped_pct: float = 0.0):
    """Merljivi pokazatelji kvaliteta (bez izmišljenih indeksa)."""
    factors, notes = [], []
    n = sig.size

    # 1) saturacija ADC-a
    lo, hi = float(np.min(sig)), float(np.max(sig))
    rail_hits = max(int((sig == lo).sum()), int((sig == hi).sum()))
    clipped = rail_hits / n * 100
    if clipped > 2:
        notes.append(f"{clipped:.1f}% uzoraka na ravni ADC opsega ({lo:.0f}/{hi:.0f}) → verovatna saturacija (smanji pojačanje)")
    factors.append(max(0.0, 1 - min(clipped, 20) / 20))

    # 2) šum: udeo snage iznad 40 Hz
    f, pxx = sps.welch(sig - np.mean(sig), fs=fs, nperseg=min(1024, n))
    tot = np.trapezoid(pxx, f) or 1e-12
    hf = f > 40
    hf_pct = float(np.trapezoid(pxx[hf], f[hf]) / tot * 100) if hf.sum() > 1 else 0.0
    if hf_pct > 15:
        notes.append(f"HF šum {hf_pct:.0f}% snage >40 Hz (loša kontakt elektroda / pomeranje)")
    factors.append(max(0.0, 1 - hf_pct / 30))

    # 3) 50 Hz smetnja (mreža)
    pl_pct = 0.0
    if fs > 110:
        band = (f >= 48) & (f <= 52)
        if band.sum():
            pl_pct = float(np.trapezoid(pxx[band], f[band]) / tot * 100)
            if pl_pct > 5:
                notes.append(f"izražena smetnja na 50 Hz ({pl_pct:.0f}% snage)")

    # 4) baseline wander (<0.5 Hz)
    f2, p2 = sps.welch(sig - np.mean(sig), fs=fs, nperseg=min(2048, n))
    bw = (f2 > 0.01) & (f2 < 0.5)
    bw_pct = float(np.trapezoid(p2[bw], f2[bw]) / (np.trapezoid(p2, f2) or 1e-12) * 100)
    if bw_pct > 40:
        notes.append(f"izražen baseline wander ({bw_pct:.0f}% snage <0.5 Hz) — normalizuj disanje")
    factors.append(max(0.0, 1 - bw_pct / 70))

    # 5) artefakti u RR
    art_pct = (rr_info["n_phys_out"] + rr_info["n_outlier"]) / max(len(rr_info["rr_all"]), 1) * 100
    if art_pct > 5:
        notes.append(f"{art_pct:.1f}% RR intervala odbačeno kao artefakt/ektopija")
    factors.append(max(0.0, 1 - art_pct / 20))

    # 6) uspešnost delineacije
    r_ok = int(np.isfinite(np.asarray(waves.get("ECG_R_Onsets", [np.nan]))).sum())
    delin_pct = r_ok / max(len(rp), 1) * 100
    if delin_pct < 60:
        notes.append(f"delineacija QRS-a uspela na {delin_pct:.0f}% beatova")
    factors.append(delin_pct / 100)

    # 7) deo snimka koji aplikacija nije upisala (pauze zbog šuma/saturacije).
    #    Snimak sa 22% odbačenog vremena ne sme da nosi oznaku "ODLIČAN".
    if dropped_pct > 1:
        notes.append(f"{dropped_pct:.0f}% snimka je odbačeno u aplikaciji (pauze zbog šuma/saturacije) — "
                     "ti delovi nisu analizirani")
        factors.append(max(0.0, 1 - min(dropped_pct, 40) / 40))

    score = int(round(np.mean(factors) * 100))
    label = "ODLIČAN" if score >= 85 else "DOBAR" if score >= 70 else "ZADOVOLJAVAJUĆI" if score >= 50 else "LOŠ"
    return {
        "score": score,
        "label": label,
        "clipped_pct": clipped,
        "hf_noise_pct": hf_pct,
        "powerline_pct": pl_pct,
        "baseline_wander_pct": bw_pct,
        "rr_artifact_pct": art_pct,
        "delineation_pct": delin_pct,
        "notes": notes,
    }


def interpret(hr: dict, iv: dict, hrv_t: dict, hrv_f: dict, qual: dict, sampling: dict,
              mb: dict | None = None, extra: dict | None = None,
              consensus: dict | None = None) -> list[str]:
    out = []
    cons = consensus or {}
    c_pr = cons.get("pr") or {}
    c_qrs = cons.get("qrs") or {}
    c_qt = cons.get("qt") or {}

    hr_med = hr.get("median", 0)
    rr_rejected = float(qual.get("rr_artifact_pct") or 0.0)
    weak_ac = hr.get("periodicity_ok") is False
    _ref = hr.get("hr_reference")
    _ref_txt = f"{_ref:.0f} bpm" if _ref else "n/a"
    _ref_src = hr.get("reference_source") or "kontrolna metoda"
    # Puls je verodostojan kada se brojanje udara i periodičnost signala slažu.
    unreliable = (hr.get("cross_check_ok") is False) or bool(hr.get("correction_reverted"))
    # Fini detalji (oblik otkucaja) su nepouzdani i kada je puls tačan.
    qrs_spread = float(iv["qrs"].get("qrs_iqr_spread_ms") or 0.0)
    morph_unreliable = qrs_spread > 30.0
    if morph_unreliable:
        out.append(f"⚠ Fini detalji nisu pouzdani: oblik QRS se menja od otkucaja do otkucaja ±{qrs_spread:.0f} ms "
                   "(šum/pomeranje elektroda). Broj otkucaja je verodostojan, ali PR/QRS/QT/HRV se NE tumače "
                   "klinički dok se ne snimi čist signal.")
    if unreliable:
        causes = []
        if hr.get("cross_check_ok") is False:
            causes.append(f"brojanje R talasa ({hr.get('hr_detection', 0):.0f} bpm) i {_ref_src} "
                          f"({_ref_txt}) se ne slažu")
        if hr.get("correction_reverted"):
            causes.append("korekcija lažnih udara je poništena (periodičnost nije bila pouzdana)")
        out.append("⚠ BROJ OTKUCAJA I RITAM NISU POUZDANI: " + "; ".join(causes) +
                   ". Najčešći uzrok je šum ili slab kontakt elektroda — ponovi snimanje u miru, "
                   "sa čvrstim kontaktom elektroda, pre nego što se osloniš na ritam.")
    elif weak_ac:
        out.append(f"Brojanje udara ({hr.get('hr_detection', 0):.0f} bpm) i {_ref_src} ({_ref_txt}) "
                   "se slažu, pa je broj otkucaja verodostojan; autokorelacija je slaba zbog šuma, "
                   "pa oblik otkucaja ostaje neizvestan.")
    if rr_rejected > 30:
        out.append(f"⚠ {rr_rejected:.0f}% RR intervala je odbačeno kao artefakt — broj otkucaja je proveren "
                   "na dva načina, ali ritam (varijabilnost) uzmi sa rezervom.")
    if hr_med:
        if unreliable:
            out.append(f"HR: {hr_med:.0f} bpm (procena iz brojanja) — vrednost NIJE pouzdana, vidi upozorenje")
        elif hr_med < 50:
            out.append(f"HR {hr_med:.0f} bpm — izražena bradikardija (ispod 50)")
        elif hr_med < 60:
            out.append(f"HR {hr_med:.0f} bpm — bradikardija (može biti normalno kod sportista, u miru)")
        elif hr_med <= 100:
            out.append(f"HR {hr_med:.0f} bpm — normalna frekvencija u miru (60–100)")
        else:
            out.append(f"HR {hr_med:.0f} bpm — tahikardija (iznad 100)")
    if hr.get("spurious_removed"):
        out.append(f"  (pre brojanja uklonjeno {hr['spurious_removed']} lažnih udara — T talas/šum je ličio na R)")

    pr = iv["pr"]
    # --- PR: meri se na medijalnom beatu; tumači SAMO ako je merenje stabilno
    mb = mb or {}
    p_med = (mb.get("p") or {})
    p_cov = (pr.get("n") or 0) / max(int(hr.get("beats") or 1), 1) * 100.0
    p_snr = float(p_med.get("snr") or 0.0)
    pr_mb = p_med.get("pr_ms")
    p_ok = bool(p_med.get("present")) and p_snr >= 4.0
    # Konsenzus (particije udara / segmenti) je primarni izvor — pokazuje da li se merenje
    # menja kroz snimak. Bez njega ostaje pojedinačni medijalni beat uz SNR ≥ 4.
    pr_cons = c_pr.get("median") if c_pr.get("ok") else None
    pr_used = pr_cons if pr_cons else (float(pr_mb) if (pr_mb is not None and np.isfinite(pr_mb)
                                                        and p_ok and not morph_unreliable) else None)
    if pr_used is not None:
        pm = float(pr_used)
        _src = (f"  (konsenzus {c_pr.get('n_ok')}/{c_pr.get('n_total')} grupa, "
                f"raspon {c_pr.get('min_ms'):.0f}–{c_pr.get('max_ms'):.0f} ms)" if pr_cons else "")
        # RANGE_PR je u sekundama, a pr_mb u milisekundima
        if pm < RANGE_PR[0] * 1000 - 5.0:
            out.append(f"PR {pm:.0f} ms — kratak (<120 ms); pre-eksitacija (WPW) se ne može tvrditi jednim "
                       "odvodom — potreban 12-kanalni ECG" + _src)
        elif abs(pm - RANGE_PR[0] * 1000) <= 5.0:
            out.append(f"PR {pm:.0f} ms — na donjoj granici normale (120–200 ms); razlika je unutar "
                       "tačnosti merenja (±5 ms)" + _src)
        elif pm > RANGE_PR[1] * 1000 + 5.0:
            out.append(f"PR {pm:.0f} ms — produžen (>200 ms), obrazac AV bloka I stepena" + _src)
        else:
            out.append(f"PR {pm:.0f} ms — normalan (120–200 ms)" + _src)
    elif c_pr.get("n_ok") is not None:
        _det = ""
        if c_pr.get("n_ok"):
            _det = (f" (izmereno u {c_pr.get('n_ok')}/{c_pr.get('n_total')} grupa, raspon "
                    f"{c_pr.get('min_ms'):.0f}–{c_pr.get('max_ms'):.0f} ms)")
        out.append(f"PR se NE tumači: {c_pr.get('reason') or 'merenje nije stabilno'}{_det}. "
                   "Početak P talasa se ne može odrediti dovoljno tačno — vrednost se prikazuje samo "
                   "kao merenje, bez zaključka o provođenju.")
    elif pr.get("n"):
        out.append(f"PR {pr['median']*1000:.0f} ms (beat-po-beat) — NE tumači se: P talas pouzdano nađen u "
                   f"{p_cov:.0f}% udara (SNR {p_snr:.1f}); potrebno ≥50% i SNR ≥4. Vrednost je prikazana samo "
                   "kao merenje, bez zaključka o provođenju.")
    else:
        out.append("PR nije izmeren — P talas nije nađen")

    qrs = iv["qrs"]
    # konsenzus je primaran i za širinu QRS (isti medijalni beat preko više grupa)
    qrs_cons = c_qrs.get("median") if c_qrs.get("ok") else None
    qrs_mb = qrs_cons or mb.get("qrs_ms")
    qrs_pb = (qrs.get("median") or 0) * 1000
    qrs_agree = bool(qrs_mb and qrs_pb and abs(qrs_mb - qrs_pb) <= 40)
    if qrs_mb:
        # primarno merenje: medijalni beat iz 0.5–75 Hz (40 Hz low-pass širi QRS)
        _wide_claim = qrs_mb > RANGE_QRS_MAX * 1000
        if _wide_claim and qrs_agree:
            out.append(f"QRS {qrs_mb:.0f} ms — širok (>120 ms); oba merenja se slažu (medijalni beat i "
                       "beat-po-beat). Moguć blok grane ili komorski ritam, ali se TIP ne može odrediti "
                       "jednim odvodom — potreban 12-kanalni ECG")
        elif _wide_claim:
            out.append(f"QRS {qrs_mb:.0f} ms (medijalni beat; beat-po-beat {qrs_pb:.0f} ms) — širok, ali se "
                       "širina ne može potvrditi drugom metodom → potreban 12-kanalni ECG")
        elif unreliable or morph_unreliable:
            out.append(f"QRS {qrs_mb:.0f} ms (medijalni beat) — širina se NE tumači ("
                       + ("detekcija udara nepouzdana" if unreliable else "oblik se menja od udara do udara")
                       + ")")
        elif qrs_mb < 80:
            # normalan opseg odraslih je 74–114 ms (AHA Part III); jednoodvodno merenje na 192 Hz
            # je sistemski uže od globalnog QRS-a, pa se vrednost NE klasifikuje kao „uzak"
            out.append(f"QRS {qrs_mb:.0f} ms — izmereno (jednoodvodno merenje; globalni QRS je po pravilu "
                       "duži, pa se vrednost prati kao trend, ne tumači kao uzak QRS)")
        else:
            out.append(f"QRS {qrs_mb:.0f} ms — normalan (60–120 ms)"
                       + (f"  (beat-po-beat metoda daje {qrs_pb:.0f} ms — obuhvata i okolne talase)"
                          if not qrs_agree else ""))
    elif qrs.get("n") and unreliable:
        out.append(f"QRS {qrs['median']*1000:.0f} ms (izmereno — ne tumači se, detekcija udara nepouzdana)")
    elif qrs.get("n") and morph_unreliable:
        out.append(f"QRS {qrs['median']*1000:.0f} ms (±{qrs_spread:.0f} ms od beata do beata) — "
                   "morfologija se ne tumači (šum/pomeranje elektroda), nije dokaz bloka grane")
    elif qrs.get("n"):
        m = qrs["median"]
        if m > RANGE_QRS_MAX:
            out.append(f"QRS {m*1000:.0f} ms — širok (>120 ms), moguć blok grane ili komorski ritam")
        elif m < 0.08:
            out.append(f"QRS {m*1000:.0f} ms — izmereno (jednoodvodno; ne tumači se kao uzak QRS — "
                       "globalni QRS je duži, vrednost služi za trend)")
        else:
            out.append(f"QRS {m*1000:.0f} ms — normalan (60–120 ms)")
        if qrs.get("qrs_iqr_spread_ms", 0) > 30:
            out.append(f"  QRS se menja od beata do beata (±{qrs['qrs_iqr_spread_ms']:.0f} ms) → verovatno šum, ne morfologija")

    qt, qtcb, qtcf = iv["qt"], iv["qtc_bazett"], iv["qtc_fridericia"]
    # QT/QTc iz istog izvora kao medicinska sekcija (medijalni beat / konsenzus) — ranije je
    # ovde korišćen beat-po-beat (dwt) pa je isti nalaz imao QTc 433 i QTc 319 ms.
    qt_cons = c_qt.get("median") if c_qt.get("ok") else None
    _tmb = mb.get("t") or {}
    qt_mb = _tmb.get("qt_ms") if _tmb.get("reliable") else None
    qt_show = qt_cons or qt_mb
    rr_med_s = (float(hr.get("median_rr_ms") or 0) / 1000.0) or (60.0 / hr_med if hr_med else None)
    # QT/QTc: prikazuje se IZMERENO, bez pragova i bez zaključka. Dve priznate metode (tangenta/prag)
    # razlikuju se u literaturi ~10 ms, a kod nas medijalni beat i beat-po-beat umeju da se razilaze
    # >100 ms — pa QTc nije valjan osnov ni za „produžen" ni za „u granicama" dok se ne validira.
    if qt_show:
        _qsrc = "konsenzus grupa" if qt_cons else "medijalni beat"
        _qt_dwt = (qt.get("median") or 0) * 1000
        _delta = abs(qt_show - _qt_dwt) if _qt_dwt else 0.0
        out.append(f"QT {qt_show:.0f} ms — izmereno ({_qsrc}); QT/QTc se NE tumači"
                   + (f" (metode se razilaze {_delta:.0f} ms)" if _delta > 40
                      else " (merenje nije validirano na ovom hardveru)"))
    elif qt.get("n"):
        out.append(f"QT {qt['median']*1000:.0f} ms (beat-po-beat) — merenje nije stabilno po grupama; "
                   "QT/QTc se ne tumači")
    else:
        out.append("QT/QTc nije izmeren — T talas nije pouzdano detektovan")

    if hrv_t.get("rmssd_ms") and (unreliable or morph_unreliable or rr_rejected > 30):
        out.append(f"HRV: SDNN {hrv_t['sdnn_ms']:.0f} ms, RMSSD {hrv_t['rmssd_ms']:.0f} ms "
                   "(ne tumači se — signal ima artefakte; ponovi snimak sa čistim kontaktom elektroda)")
    elif hrv_t.get("rmssd_ms"):
        rmssd = hrv_t["rmssd_ms"]
        if rmssd >= 40:
            note = "dobra vagalna aktivnost / oporavak"
        elif rmssd >= 20:
            note = "uobičajeno za mirno stanje"
        else:
            note = "niska varijabilnost — stres, umor, bolest ili artefakti"
        out.append(f"HRV: SDNN {hrv_t['sdnn_ms']:.0f} ms, RMSSD {rmssd:.0f} ms, pNN50 {hrv_t['pnn50_pct']:.1f}% — {note}")
    if hrv_f.get("lf_hf") and not (unreliable or morph_unreliable):
        # LF/HF je odnos snaga dve trake, ne mera simpatičko-parasimpatičke ravnoteže (Billman 2013)
        _lf_q = " — odnos snaga (nije mera autonomne ravnoteže)"
        if float(hrv_f.get("duration_s") or 0) < 300:
            _lf_q += "; snimak kraći od 5 min → orijentaciono"
        out.append(f"LF/HF {hrv_f['lf_hf']:.2f} (LF {hrv_f['lf_ms2']:.0f} ms², HF {hrv_f['hf_ms2']:.0f} ms²)"
                   + _lf_q)

    if qual["rr_artifact_pct"] > 10:
        out.append(f"{qual['rr_artifact_pct']:.0f}% RR intervala je odbačeno — ritam se tumači sa rezervom")

    if sampling.get("dropped_pct", 0) and sampling["dropped_pct"] > 1:
        out.append(
            f"{sampling['dropped_pct']:.0f}% snimka ({sampling.get('dropped_s', 0):.0f} s) aplikacija je "
            "preskočila jer je signal bio šumovit ili je elektroda gubila kontakt — ti delovi su izuzeti "
            "iz analize i NISU srčane pauze"
        )
    if sampling.get("mismatch_pct", 0) and sampling["mismatch_pct"] > 5:
        out.append(
            f"Nominalna fs {sampling['nominal_hz']:.0f} Hz vs izmerena {sampling['measured_hz']:.1f} Hz "
            f"({sampling['mismatch_pct']:.1f}%) — verovatno izgubljeni uzorci u serijskom prenosu; "
            "intervali su računati iz timestamp ose, pa greška ne utiče na njih"
        )
    return out


# ------------------------------------------------- laičko tumačenje (objašnjenje) ----
def layman_state(result: dict) -> list[str]:
    """Opis stanja običnim jezikom: šta je snimak pokazao, šta to obično znači i šta se
    ovim snimkom NE vidi.

    Namerno bez skraćenica (PR/QRS/QTc) i bez dijagnoze — čita se kao objašnjenje koje bi
    lekar dao pacijentu, a ne kao nalaz.
    """
    hr = result.get("heart") or {}
    med = result.get("medical") or {}
    cons = result.get("consensus") or {}
    ex = result.get("extra") or {}
    q = result.get("quality") or {}
    hr_med = float(hr.get("median") or 0)
    ok_pulse = (hr.get("cross_check_ok") is not False) and not hr.get("correction_reverted")
    out = []

    # --- 1) ukupna slika jednom rečenicom
    if ok_pulse and hr_med:
        if hr_med < 60:
            s = (f"Ukupna slika: srce je kucalo sporijim tempom nego što je uobičajeno "
                 f"(oko {hr_med:.0f} puta u minutu)")
        elif hr_med <= 100:
            s = (f"Ukupna slika: srce je kucalo mirno i uobičajenim tempom "
                 f"(oko {hr_med:.0f} puta u minutu)")
        else:
            s = (f"Ukupna slika: srce je kucalo bržim tempom nego u miru "
                 f"(oko {hr_med:.0f} puta u minutu)")
    else:
        s = "Ukupna slika: broj otkucaja se iz ovog snimka ne može pouzdano odrediti"
    e = ex.get("ectopics") or {}
    rsp = ex.get("respiration") or {}
    rng = e.get("rr_range_ms")
    if rng and e.get("median_rr_ms"):
        var = (rng[1] - rng[0]) / e["median_rr_ms"] * 100
        # Ujednačeno usporenje/ubrzanje u ritmu disanja je normalna pojava (sinusna aritmija);
        # „nepravilan" treba reći samo kada razmaci skaču bez obrasca ili ima ektopija/pauza.
        resp_like = (float(rsp.get("dominance_pct") or 0) >= 30
                     and not e.get("premature") and not e.get("pauses"))
        if var < 15:
            s += ", a razmaci između otkucaja su bili ujednačeni"
        elif resp_like:
            s += (", a razmaci između otkucaja su se menjali u ritmu disanja "
                  "(to je uobičajeno i nije nepravilan ritam)")
        elif var < 25:
            s += ", a razmaci između otkucaja su malo varirali"
        else:
            s += ", a razmaci između otkucaja su bili nepravilni"
        if e.get("premature"):
            s += f"; {e['premature']} otkucaja je došlo ranije nego što je red"
        if e.get("pauses"):
            s += f"; bilo je i {e['pauses']} stanke duže od uobičajene"
    out.append(s + ".")

    # --- 2) kako srce provodi signal (bez skraćenica)
    pr = cons.get("pr") or {}
    qrs = cons.get("qrs") or {}
    qt = cons.get("qt") or {}
    bits = []
    if pr.get("ok"):
        v = float(pr["median"])
        if v < 120:
            bits.append(f"signal iz pretkomora stiže u komore brže nego obično ({v:.0f} umesto 120–200 ms)")
        elif v > 200:
            bits.append(f"signal iz pretkomora stiže u komore sporije nego obično ({v:.0f} ms, uobičajeno 120–200)")
        else:
            bits.append(f"signal iz pretkomora stiže u komore u uobičajenom vremenu ({v:.0f} ms)")
    if qrs.get("ok"):
        v = float(qrs["median"])
        bits.append(f"komore se skupljaju {v:.0f} ms "
                    + ("(izmereno jednim odvodom — stvarna širina je po pravilu malo veća)"
                       if v <= 120 else "(duže nego obično, ali širinu treba potvrditi 12-kanalnim)"))
    if qt.get("ok"):
        bits.append("vreme resetovanja srca je izmereno, ali se ovim snimkom ne tumači "
                    "(za to treba kalibrisan 12-kanalni EKG)")
    if bits:
        out.append("Kako srce provodi signal: " + "; ".join(bits) + ".")

    # --- 3) šta to stanje obično znači
    anomal = med.get("anomalies") or []
    if not anomal:
        out.append("Ovakav nalaz odgovara mirnom, pravilnom radu srca — u ovom snimku nema izmerenog "
                   "odstupanja u brzini, u ritmu ni u provođenju signala kroz srce.")
    else:
        plain = []
        for a in anomal:
            al = a.lower()
            if "bradikard" in al:
                plain.append("puls je sporiji nego što se očekuje u miru (kod istreniranih ljudi je to često "
                             "normalno, ali ako prati vrtoglavicu ili umor — pokazati lekaru)")
            elif "tahikard" in al:
                plain.append("puls je brži nego što se očekuje u miru (napor, stres, kafa, temperatura ili "
                             "lekovi su najčešći razlozi; ako je tako i posle odmora — pokazati lekaru)")
            elif "preuranjen" in al:
                plain.append("poneki otkucaj je došao ranije nego što je red — obično bezopasno, ali se tip "
                             "ne može odrediti jednim odvodom")
            elif "pauz" in al:
                plain.append("bila je stanka između otkucaja duža od uobičajene — vredi proveriti sa lekarom")
            elif "pr " in al and "kratak" in al:
                plain.append("vreme od pretkomore do komore je kratko (to se pouzdano vidi samo na "
                             "12-kanalnom snimku)")
            elif "pr " in al or "av blok" in al:
                plain.append("vreme od pretkomore do komore je duže nego obično (sprovod kroz srce je malo "
                             "usporen) — vredi pokazati lekaru")
            elif "qrs" in al:
                plain.append("komore se skupljaju duže nego obično; tip se ne može odrediti jednim odvodom")
            elif "qtc" in al:
                plain.append("vreme 'resetovanja' srca je izmereno, ali se ovim snimkom ne tumači "
                             "(potreban kalibriran 12-kanalni EKG)")
            else:
                plain.append(a)
        out.append("Šta izmerene stvari znače: " + "; ".join(plain) + ".")

    # --- 4) šta se ovim snimkom NE vidi (i zašto to nije „sve u redu")
    out.append("Ovim snimkom se NE vidi: da li je srčani mišić zadebljao, da li je nekada postojao infarkt, "
               "kako srce radi pod naporom i kako struja prolazi gledano iz drugih uglova — za to je potreban "
               "12-kanalni EKG. Normalan snimak ne isključuje bolest koronarnih arterija.")

    # --- 5) šta je u ovom snimku ostalo nepročitano
    if not pr.get("ok"):
        if pr.get("n_ok"):
            out.append("Vreme provođenja iz pretkomora ovde nije bilo moguće pročitati: merenje se na "
                       "različitim delovima snimka nije poklopilo, što znači da je P talas bio preslab. "
                       "To je osobina snimka (jedan odvod, mala amplituda), ne nalaz o srcu.")
        else:
            out.append("Vreme provođenja iz pretkomora ovde nije bilo moguće pročitati jer se P talas "
                       "(mali talas pre svakog otkucaja) ne vidi dovoljno jasno. To je osobina snimka, "
                       "ne nalaz o srcu.")
    if float(q.get("rr_artifact_pct") or 0) > 20:
        out.append("Deo otkucaja je odbačen zbog šuma, pa se brojevi odnose na obrađeni deo snimka.")
    return out


def layman(result: dict) -> list[str]:
    """Isti nalaz, ali bez stručnih izraza — da se odmah razume šta piše i šta dalje.

    Namerno izbegava skraćenice (PR/QRS/QTc/SDNN) i ne iznosi dijagnozu; objašnjava
    i kada je nalaz nepouzdan (šum, odbačeni otkucaji) i šta konkretno popraviti.
    """
    hr = result.get("heart") or {}
    q = result.get("quality") or {}
    s = result.get("sampling") or {}
    iv = result.get("intervals") or {}
    dur = float(s.get("duration_s") or 0)
    beats = int(hr.get("beats") or 0)
    hr_med = float(hr.get("median") or 0)
    rr_rejected = float(q.get("rr_artifact_pct") or 0)
    hf_noise = float(q.get("hf_noise_pct") or 0)
    filt_std = float((result.get("signal") or {}).get("filtered_std") or 0)

    out = [f"Snimak je trajao {dur:.0f} sekundi i u njemu je prepoznato {beats} otkucaja srca."]
    drop_pct = float((result.get("sampling") or {}).get("dropped_pct") or 0)
    if drop_pct > 1:
        out.append(f"Napomena: {drop_pct:.0f}% vremena softver nije upisao jer je signal bio šumovit ili "
                   "je elektroda gubila kontakt. Ti delovi su izuzeti iz analize i NISU prekid rada srca.")
    pulse_ok = (hr.get("cross_check_ok") is not False) and not hr.get("correction_reverted") and beats >= 10
    if pulse_ok and hr_med:
        out.append(f"Srce je u proseku kucalo {hr_med:.0f} puta u minutu.")
        if 60 <= hr_med <= 100:
            out.append("To je uobičajen broj otkucaja za osobu koja miruje.")
        elif hr_med < 60:
            out.append("To je sporije od uobičajenog (ispod 60 u minutu). Kod istreniranih ljudi je u miru "
                       "to često normalno — ali ako imaš vrtoglavicu, umor ili kratak dah, pokaži snimak lekaru.")
        else:
            out.append("To je brže od uobičajenog (iznad 100 u minutu). U miru to obično znači napor, stres, "
                       "povišenu temperaturu ili previše kafe — ako je i posle odmora tako, pokaži snimak lekaru.")
        if rr_rejected > 30:
            out.append(f"Broj otkucaja je proveren na dva načina i poklapa se, ali je {rr_rejected:.0f}% otkucaja "
                       "moralo biti odbačeno zbog šuma — zato ovaj broj uzmi sa malom rezervom.")
    else:
        out.append("Broj otkucaja se iz ovog snimka NE MOŽE pouzdano odrediti. Uzrok je najčešće šum: "
                   "veliki deo otkucaja je pri obradi odbačen jer nije ličio na ispravan otkucaj.")

    spread = float((iv.get("qrs") or {}).get("qrs_iqr_spread_ms") or 0)
    p_med = ((result.get("median_beat") or {}).get("p") or {})
    p_cov = (iv.get("pr") or {}).get("n", 0) / max(int(hr.get("beats") or 1), 1) * 100.0
    p_ok = bool(p_med.get("present")) and float(p_med.get("snr") or 0) >= 4
    # konsenzus: isti nalaz izmeren više puta (delovi snimka) — jedini dokaz da je merenje stabilno
    cons_pr = ((result.get("consensus") or {}).get("pr") or {})
    pr_val = (result.get("medical") or {}).get("pr_used_ms") or (cons_pr.get("median") if cons_pr.get("ok") else None)
    if pulse_ok and cons_pr.get("ok") and pr_val:
        if pr_val < 120:
            _pv = "kraće od uobičajenog (120–200)"
        elif pr_val > 200:
            _pv = "duže od uobičajenog (120–200)"
        else:
            _pv = "u granicama uobičajenog (120–200)"
        out.append(f"Vreme od pretkomore do komore (PR) je {pr_val:.0f} milisekundi — {_pv}. "
                   f"Izmereno je više puta, na {cons_pr.get('n_ok')} različitih delova snimka, i svaki put isto "
                   f"(razlika najviše {cons_pr.get('max_ms', 0) - cons_pr.get('min_ms', 0):.0f} ms), pa se tom "
                   "broju može verovati.")
    if pulse_ok and spread <= 30 and rr_rejected <= 30 and (p_ok or cons_pr.get("ok")):
        out.append("Oblik otkucaja je bio ujednačen, a mali talas pre svakog otkucaja (P talas) se vidi "
                   "dovoljno da se izmeri na više delova snimka — zato se mogu čitati i fini detalji "
                   "(trajanja i varijabilnost ritma).")
    elif pulse_ok and spread <= 30 and rr_rejected <= 30:
        if cons_pr.get("n_ok") and cons_pr.get("max_ms") and cons_pr["max_ms"] > cons_pr["min_ms"]:
            out.append(f"P talas se ne vidi dovoljno jasno, a merenje na različitim delovima snimka se ne slaže "
                       f"({cons_pr.get('min_ms'):.0f}–{cons_pr.get('max_ms'):.0f} ms), tako da se vreme od "
                       "pretkomore do komore (PR) ne tumači.")
        elif cons_pr.get("n_ok"):
            out.append("P talas je preslab da bi se vreme od pretkomore do komore (PR) izmerilo u dovoljnom "
                       "broju delova snimka, pa se taj interval ne tumači.")
        else:
            out.append("Oblik otkucaja je bio ujednačen, pa je broj otkucaja i trajanje QRS-a pouzdano. "
                       "P talas se ne vidi dovoljno jasno, tako da se fini intervali (PR) ne tumače.")
    else:
        out.append("Oblik pojedinačnih otkucaja se menjao od otkucaja do otkucaja, što je znak šuma. Zato se "
                   "delovi nalaza koji zavise od oblika otkucaja (PR, QRS, QT, HRV) ne mogu pouzdano pročitati. "
                   "To NE znači da sa srcem nešto nije u redu — znači da snimak nije bio dovoljno čist.")

    if hf_noise > 12:
        out.append(f"Snimak je bio šumovit ({hf_noise:.0f}% snage u visokim frekvencijama). Najčešći razlozi: "
                   "elektrode nisu dobro nalepljene ili se koža znoji, kabl se pomerao, ili je u blizini bio "
                   "telefon/punjač koji pravi smetnje.")
    if filt_std and filt_std < 15:
        out.append("Razlika između najniže i najviše vrednosti signala je mala, pa je i sam signal slab "
                   "(elektrode možda nisu na pravom mestu ili je pojačanje uređaja malo).")
    if rr_rejected > 30:
        out.append(f"{rr_rejected:.0f}% otkucaja je odbačeno kao šum — zato se na broj otkucaja i ritam iz ovog "
                   "snimka ne oslanjaj.")
    if dur and dur < 30:
        out.append("Snimak je kraći od 30 sekundi — za pouzdan ritam i varijabilnost srca snimi bar 1–2 minuta.")

    # opis stanja običnim jezikom (šta je viđeno, šta to znači, šta se ne vidi) — ide pre saveta
    out.extend(layman_state(result))

    out.append("Šta konkretno pomoći: sedi mirno 1–2 minuta pre snimanja, očisti i osuši kožu, čvrsto nalepl "
               "elektrode, ne pomeraj kabl i skloni telefon i punjač od uređaja. Zatim snimi 30 sekundi i pusti "
               "analizu ponovo.")
    out.append("Ovaj nalaz je informativan i NIJE medicinska dijagnoza. Ako imaš tegobe (lupanje srca, "
               "vrtoglavica, gušenje, bol u grudima), pokaži snimak lekaru.")
    return out


# --------------------------------------------------------------- crtanje ----
def plot_report(sig, t, clean, rp, rr_info, hrv_t, out_png, title_suffix=""):
    rr_ms = rr_info["rr_all"] * 1000
    rr_ok = rr_info["mask_clean"]
    clean_rr = rr_info["rr_all"][rr_ok] * 1000
    beat_t = t[rp][1:]

    fig, ax = plt.subplots(3, 2, figsize=(16, 12))
    fs_est = 1 / np.median(np.diff(t)) if len(t) > 2 else 1
    win = int(min(len(sig), max(200, 10 * fs_est)))

    ax[0, 0].plot(t[:win], sig[:win], color="#333", lw=0.9)
    ax[0, 0].set_title(f"Sirov signal — prvih {win/fs_est:.0f} s")
    ax[0, 0].set_xlabel("vreme (s)")
    ax[0, 0].set_ylabel("ADC")

    ax[0, 1].plot(t[:win], clean[:win], color=SERIES_COLOR, lw=1.0)
    m = rp[rp < win]
    ax[0, 1].plot(t[m], clean[m], "ro", ms=5, label="R vrhovi")
    ax[0, 1].set_title("Filtriran signal + detektovani R vrhovi")
    ax[0, 1].set_xlabel("vreme (s)")
    ax[0, 1].legend(loc="upper right", fontsize=8)

    ax[1, 0].plot(t, clean, color=SERIES_COLOR, lw=0.6)
    ax[1, 0].plot(t[rp], clean[rp], "ro", ms=3)
    ax[1, 0].set_title(f"Ceo snimak — {len(rp)} detektovanih beatova")
    ax[1, 0].set_xlabel("vreme (s)")

    ax[1, 1].plot(beat_t[rr_ok], clean_rr, "o-", ms=3, color="#146c43", lw=0.8)
    ax[1, 1].plot(beat_t[~rr_ok], rr_ms[~rr_ok], "x", color="#c00", ms=5, label="odbačeno (artefakt)")
    ax[1, 1].axhline(np.median(clean_rr), color="#c00", ls="--", lw=1, label=f"medijana {np.median(clean_rr):.0f} ms")
    ax[1, 1].set_title("RR tachogram")
    ax[1, 1].set_xlabel("vreme (s)")
    ax[1, 1].set_ylabel("RR (ms)")
    ax[1, 1].legend(fontsize=8)

    ax[2, 0].hist(clean_rr, bins=20, color="#6f42c1", alpha=0.8, edgecolor="white")
    ax[2, 0].axvline(np.median(clean_rr), color="#c00", ls="--", label=f"medijana {np.median(clean_rr):.0f} ms")
    ax[2, 0].set_title("Raspodela RR intervala")
    ax[2, 0].set_xlabel("RR (ms)")
    ax[2, 0].legend(fontsize=8)

    if clean_rr.size > 3:
        ax[2, 1].scatter(clean_rr[:-1], clean_rr[1:], s=12, alpha=0.6, color="#d63384")
        lo, hi = clean_rr.min(), clean_rr.max()
        ax[2, 1].plot([lo, hi], [lo, hi], "k--", lw=0.8, alpha=0.6)
        ax[2, 1].set_title(f"Poincaré (SD1 {hrv_t.get('sd1_ms', 0):.0f} ms, SD2 {hrv_t.get('sd2_ms', 0):.0f} ms)")
        ax[2, 1].set_xlabel("RRₙ (ms)")
        ax[2, 1].set_ylabel("RRₙ₊₁ (ms)")

    for a in ax.ravel():
        a.grid(alpha=0.25)
    hr = 60 / np.median(rr_info["rr_all"][rr_ok]) if rr_ok.any() else 0
    fig.suptitle(f"ECG izveštaj — HR {hr:.0f} bpm, {t[-1]:.0f} s, {len(rp)} beatova{title_suffix}", fontsize=14)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_intervals(sig, t, rp, waves, iv, out_png, seg_seconds=10):
    n = len(rp)

    def arr(key):
        v = np.asarray(waves.get(key, np.full(n, np.nan)), dtype=float)
        return np.concatenate([v, np.full(n - v.size, np.nan)])[:n] if v.size < n else v[:n]

    r_on, r_off = arr("ECG_R_Onsets"), arr("ECG_R_Offsets")
    p_on, p_pk = arr("ECG_P_Onsets"), arr("ECG_P_Peaks")
    t_off = arr("ECG_T_Offsets")
    q_pk = arr("ECG_Q_Peaks")

    # --- (a) segment sa oznakama
    idx_end = int(np.searchsorted(t, t[0] + seg_seconds))
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(16, 11), gridspec_kw={"height_ratios": [1.4, 1]})
    a1.plot(t[:idx_end], sig[:idx_end], color="#333", lw=1.1, label="ECG")
    marks = [
        (p_on, "g<", "P onset"), (p_pk, "go", "P vrh"), (q_pk, "mv", "Q"),
        (r_on, "b>", "QRS onset"), (rp, "ro", "R vrh"), (r_off, "b<", "QRS offset"),
        (t_off, "y^", "T offset"),
    ]
    for values, style, lab in marks:
        sel = [int(v) for v in values if np.isfinite(v) and v < idx_end]
        if sel:
            a1.plot(t[sel], sig[sel], style, ms=7, label=lab, alpha=0.85)
    for i in range(n):
        if not np.isfinite(r_on[i]) or r_on[i] >= idx_end:
            continue
        base = sig[int(r_on[i])]
        span = float(np.ptp(sig[:idx_end]) or 1)
        if np.isfinite(p_on[i]) or np.isfinite(p_pk[i]):
            pv = p_on[i] if np.isfinite(p_on[i]) else p_pk[i]
            lab = "PR" if np.isfinite(p_on[i]) else "PR≈"
            a1.annotate(f"{lab} {t[int(r_on[i])]-t[int(pv)]:.3f}s",
                        xy=((t[int(r_on[i])] + t[int(pv)]) / 2, base - 0.06 * span),
                        ha="center", fontsize=8, color="#0a3622",
                        bbox=dict(boxstyle="round,pad=0.2", fc="#d1e7dd", ec="#0a3622", lw=0.5))
        if np.isfinite(r_off[i]):
            a1.annotate(f"QRS {t[int(r_off[i])]-t[int(r_on[i])]:.3f}s",
                        xy=((t[int(r_on[i])] + t[int(r_off[i])]) / 2, base + 0.04 * span),
                        ha="center", fontsize=8, color="#084298",
                        bbox=dict(boxstyle="round,pad=0.2", fc="#cfe2ff", ec="#084298", lw=0.5))
        if np.isfinite(t_off[i]) and t_off[i] < idx_end:
            a1.annotate(f"QT {t[int(t_off[i])]-t[int(r_on[i])]:.3f}s",
                        xy=((t[int(r_on[i])] + t[int(t_off[i])]) / 2, base - 0.16 * span),
                        ha="center", fontsize=8, color="#664d03",
                        bbox=dict(boxstyle="round,pad=0.2", fc="#fff3cd", ec="#664d03", lw=0.5))
    a1.set_title(f"Intervali na prvih {seg_seconds} s (P/QRS/T tačke iz delineacije)")
    a1.set_xlabel("vreme (s)")
    a1.set_ylabel("ADC")
    a1.legend(ncol=4, fontsize=8, loc="upper right")
    a1.grid(alpha=0.25)

    # --- (b) medijalni beat (ensemble average)
    fs_est = 1 / np.median(np.diff(t))
    pre, post = int(0.35 * fs_est), int(0.55 * fs_est)
    beats = []
    for r in rp:
        if r - pre >= 0 and r + post < len(sig):
            seg = sig[r - pre: r + post].astype(float)
            seg = seg - np.median(seg[:max(5, pre // 3)])
            beats.append(seg)
    if beats:
        B = np.vstack(beats)
        med = np.median(B, axis=0)
        xb = (np.arange(med.size) - pre) / fs_est * 1000
        a2.fill_between(xb, np.percentile(B, 25, axis=0), np.percentile(B, 75, axis=0),
                        color=SERIES_COLOR, alpha=0.18, label="IQR svih beatova")
        a2.plot(xb, med, color=SERIES_COLOR, lw=1.8, label="medijalni beat")
        for val, lab, col in ((r_on, "QRS on", "#084298"), (r_off, "QRS off", "#084298"),
                              (p_on, "P on", "#0a3622"), (t_off, "T off", "#664d03")):
            offs = [ (v - rp[i]) / fs_est * 1000 for i, v in enumerate(val) if np.isfinite(v)]
            if offs:
                xm = float(np.median(offs))
                if -pre / fs_est * 1000 <= xm <= post / fs_est * 1000:
                    a2.axvline(xm, color=col, ls=":", lw=1.2, alpha=0.8)
                    a2.text(xm, a2.get_ylim()[1], lab, rotation=90, va="top", ha="right", fontsize=8, color=col)
        a2.set_title("Medijalni beat (prosečna morfologija) — vertikalne linije: medijane detektovanih tačaka")
        a2.set_xlabel("vreme u odnosu na R vrh (ms)")
        a2.set_ylabel("ADC")
        a2.legend(fontsize=8)
        a2.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------ main ----
def analyse(input_path: str, outdir: str, prefix: str, fs_nominal: float | None) -> dict:
    log("=== ECG ANALIZA ===")
    log(f"ulaz: {input_path}")
    if not os.path.exists(input_path):
        raise ValueError(f"fajl ne postoji: {input_path}")
    sig_raw, times, meta = load_signal(input_path, fs_nominal)
    n = sig_raw.size
    if n < 500:
        raise ValueError(f"premalo uzoraka ({n}) — snimi bar 10 s")
    log(f"uzoraka: {n:,}; kolona: {meta['signal_column']}; {meta['timestamp_note']}")

    fs_fallback = float(fs_nominal or 192.0)
    t, fs_measured = build_time_axis(times, n, fs_fallback)
    duration = float(t[-1] - t[0])
    # odbaceni delovi iz aplikacije (# PAUSE/# RESUME/# GAP) — NISU srcane pauze
    pauses, gaps = parse_markers(input_path)
    dropped = dropped_intervals(pauses, gaps, duration)
    dropped_s = float(sum(b - a for a, b in dropped))
    # Uzorci koji padaju u odbacene delove. Stara aplikacija ih NIJE upisivala (rupe u
    # CSV-u), nova (2.1+) upisuje sirovo pa se izuzimaju ovde — zato fs i kvalitet idu
    # preko keep_mask (radi sa OBA formata snimka).
    keep_mask = np.ones(n, dtype=bool)
    for _a, _b in dropped:
        keep_mask &= ~((t >= _a) & (t < _b))
    n_bad_samples = int(n - keep_mask.sum())
    if n_bad_samples:
        log(f"  uzoraka u odbačenim delovima: {n_bad_samples} (izuzeti iz analize)")
    if dropped_s > 0.5:
        log(f"odbaceni delovi iz aplikacije: {len(pauses)} pauza + {len(gaps)} praznina = "
            f"{dropped_s:.1f} s ({100 * dropped_s / max(1e-6, duration):.1f}% snimka)")
    if fs_measured:
        fs_used = float(fs_nominal) if fs_nominal else fs_measured
        # Izmerena fs preko CELE ose je manja od nominalne svaki put kada je aplikacija
        # preskakala uzorke u pauzama (tih uzoraka nema u CSV-u) — zato se odstupanje
        # racuna na AKTIVNOM vremenu, inace je "22.6% izgubljenih uzoraka" lazna uzbuna.
        # fs na aktivnom delu = broj AKTIVNIH uzoraka / aktivno vreme. Radi i za stari format
        # (uzorci u pauzi nedostaju) i za novi (upisani su, pa se ne broje).
        fs_active = ((int(keep_mask.sum()) - 1) / max(1e-6, duration - dropped_s)) \
            if dropped_s > 0.5 else fs_measured
        mismatch = abs(fs_active - fs_used) / fs_used * 100 if fs_used else 0.0
    else:
        fs_used, mismatch, fs_active = fs_fallback, 0.0, None
    log(f"trajanje: {duration:.1f} s (aktivno {duration - dropped_s:.1f} s); fs nominalna={fs_nominal or '-'}; izmerena={fs_measured and round(fs_measured,1)}; aktivna={fs_active and round(fs_active,1)}; korišćena za detekciju={fs_used:.1f} Hz")
    if duration < 20:
        log("  ! snimak kraći od 20 s — HRV i intervali su nepouzdani")

    # --- obrada
    clean = nk.ecg_clean(sig_raw, sampling_rate=fs_used, method="neurokit")
    # periodičnost signala pre detekcije: služi kao referenca za RR i za kontrolu
    ac = hr_autocorr(clean, fs_used)
    rp_raw = detect_beats(clean, fs_used)
    if dropped and len(rp_raw):
        keep = np.array([not _in_intervals(float(t[p]), dropped) for p in rp_raw], dtype=bool)
        n_in_bad = int((~keep).sum())
        if n_in_bad:
            rp_raw = rp_raw[keep]
            log(f"  vrhova detektovanih u odbačenim delovima: {n_in_bad} (izbačeni)")
    # Referencu za uklanjanje T talasa uzimamo SAMO ako je periodičnost pouzdana
    # (`ok` + dovoljna korelacija). `trusted=bool(ac)` je bio bug — dict je uvek truthy,
    # pa je sa peak_corr 0.17 referenca bila 2x pogrešna i uklonjen je svaki drugi PRAVI
    # udar (16.10.2026: prijavljeno 39 bpm na snimku koji je stvarno bio 79).
    ac_trusted = bool(ac and ac.get("ok") and ac.get("peak_corr", 0.0) >= AC_MIN_PEAK_CORR)
    rp, n_double, ac_reverted = correct_double_counts(
        rp_raw, clean, (ac["period_s"] * fs_used) if ac else None, trusted=ac_trusted)
    # Korekcija se kontrolise NEZAVISNIM brojacem (anvelopa 5-25 Hz), a ne istom
    # autokorelacijom — cirkularna provera (HR vs ista pogresna referenca) je 16.10.2026.
    # "potvrdila" prepolovljen puls.
    ind = independent_hr(sig_raw, t, fs_used)
    hr_ind = float(ind["hr_bpm"]) if ind else None
    if n_double and hr_ind and len(rp) > 2:
        hr_after = float(np.median(60.0 / np.diff(t[rp])))
        ratio = hr_after / hr_ind
        if 0.35 <= ratio <= 0.65:
            rp, n_double, ac_reverted = rp_raw, 0, True
    log(f"R vrhovi: {len(rp)}" + (f" (uklonjeno {n_double} lažnih: T talas/šum)" if n_double else "")
        + ("  [korekcija poništena: periodičnost nije pouzdana]" if ac_reverted else ""))
    rr_info = rr_analysis(rp, t)
    if dropped and len(rp) > 1:
        span_ok = np.array([not _span_crosses(float(t[rp[i]]), float(t[rp[i + 1]]), dropped)
                            for i in range(len(rp) - 1)], dtype=bool)
        rr_info["mask_phys"] = rr_info["mask_phys"] & span_ok
        rr_info["mask_clean"] = rr_info["mask_clean"] & span_ok
        rr_info["n_dropped_span"] = int((~span_ok).sum())
    hr_values = 60.0 / rr_info["rr_all"][rr_info["mask_clean"]]
    hr = _stat(hr_values)
    hr["beats"] = int(len(rp))
    hr["rr_artifacts"] = rr_info["n_phys_out"] + rr_info["n_outlier"]
    hr["pauses_over_2s"] = int((rr_info["rr_all"][rr_info["mask_clean"]] > 2.0).sum())

    rr_clean = rr_info["rr_all"][rr_info["mask_clean"]]
    rr_times_clean = t[rp][1:][rr_info["mask_clean"]] if rr_info["mask_clean"].size == len(rp) - 1 else t[rp][1:]
    hrv_t = hrv_time(rr_clean)
    hrv_f = hrv_freq(rr_clean, rr_times_clean)

    method, waves, scores = delineate(clean, rp, fs_used)
    raw_iv = intervals_for_beats(waves, rp, t, fs_used)
    iv = {
        "pr": _stat(raw_iv["pr"]),
        "pr_sources": raw_iv["pr_sources"],
        "qrs": _stat(raw_iv["qrs"]),
        "qt": _stat(raw_iv["qt"]),
        "qtc_bazett": _stat(raw_iv["qtc_bazett"]),
        "qtc_fridericia": _stat(raw_iv["qtc_fridericia"]),
        "delineation_method": method,
        "delineation_scores": scores,
    }
    spread = 0.0
    if iv["qrs"].get("n"):
        lo, hi = iv["qrs"]["iqr"]
        spread = (hi - lo) * 1000
    iv["qrs"]["qrs_iqr_spread_ms"] = spread

    # --- merenje trajanja na širem opsegu i na medijalnom beatu --------------
    # 40 Hz low-pass veštački širi QRS, a pojedinačni udari umeju da promaše
    # onset/offset. Zato se QRS/P/T mere na medijalnom beatu iz 0.5–75 Hz signala.
    try:
        hi = min(75.0, fs_used / 2.0 - 2.0)
        wide = nk.signal_filter(sig_raw, sampling_rate=fs_used, lowcut=0.5, highcut=hi,
                                method="butterworth", order=4)
    except Exception:
        wide = clean
    mb_iv, mb = {}, None
    if HAS_MEDICAL:
        mb = median_beat(wide, clean, rp, fs_used)
        if mb:
            mb_iv = intervals_from_median_beat(mb)
            mb_iv["template_corr"] = round(float(mb["template_corr_median"]), 3)
            mb_iv["n_beats"] = int(mb["n_beats"])
            extra_txt = []
            if mb_iv.get("qrs_ms"):
                extra_txt.append(f"QRS {mb_iv['qrs_ms']:.0f} ms")
            prf = (mb_iv.get("p") or {}).get("pr_ms")
            if prf is not None and np.isfinite(prf):
                extra_txt.append(f"PR {prf:.0f} ms")
            if (mb_iv.get("t") or {}).get("qt_ms"):
                extra_txt.append(f"QT {mb_iv['t']['qt_ms']:.0f} ms")
            log(f"medijalni beat ({mb['n_beats']} udara, sličnost {mb['template_corr_median']:.2f}): "
                + ", ".join(extra_txt))

    # --- konsenzus merenja: isti medijalni beat preko particija udara i segmenata snimka
    # Pojedinačno merenje sa celog snimka je jedna procena; ovde se vidi i da li je stabilna.
    # Svaka dodatna analiza ima svoj try/except (pravilo: jedna pala analiza ne briše ostale).
    consensus = {}
    if HAS_SEGMENTS:
        try:
            consensus = ecg_segments.analyse(wide, clean, rp, t, fs_used, seg_s=30.0, k=4,
                                             min_beats=15, dropped=dropped)

            def _short(key):
                c = consensus.get(key) or {}
                if c.get("median") is None:
                    return "– (%s)" % (c.get("reason") or "nema merenja")
                return ("%.0f ms, %d/%d grupa, raspon %.0f–%.0f"
                        % (c["median"], c.get("n_ok", 0), c.get("n_total", 0),
                           c.get("min_ms", 0), c.get("max_ms", 0))
                        + ("" if c.get("ok") else "  ← NE slažu se"))
            log(f"konsenzus: {consensus.get('k', 0)} particija + {consensus.get('n_segments', 0)} segmenata "
                f"(od {consensus.get('seg_s') or 0:.0f} s) → PR {_short('pr')} | QRS {_short('qrs')} | QT {_short('qt')}")
            if consensus.get("state_change"):
                log(f"  ! promena stanja u snimku: HR po segmentima se menja {consensus.get('hr_range_pct')} %")
        except Exception as exc:
            log(f"  ! konsenzus merenja nije uspeo: {exc}")
            consensus = {}

    extra = {}
    if HAS_MEDICAL:
        # Svaka dodatna analiza se štiti PONAOSOB — ranije je jedna pala analiza
        # (npr. IndexError u noise_timeline) brisala i sve ostale iz izveštaja.
        extra_jobs = [
            ("ectopics", lambda: ectopics_and_pauses(
                rr_info["rr_all"][rr_info["mask_clean"]],
                (t[rp][1:][rr_info["mask_clean"]]
                 if len(rp) > 1 and rr_info["mask_clean"].size == len(rp) - 1 else None))),
            ("respiration", lambda: respiration_rate(sig_raw.astype(float), fs_used)),
            ("nonlinear", lambda: nonlinear_hrv(rr_clean, duration)),
            ("noise_timeline", lambda: noise_timeline(sig_raw.astype(float), t, fs_used)),
            ("st_trend", lambda: st_trend(wide, rp, t, fs_used)),
        ]
        for _name, _fn in extra_jobs:
            try:
                extra[_name] = _fn()
            except Exception as exc:
                log(f"  ! dodatna analiza '{_name}' nije uspela: {exc}")
    
    # Kvalitet se meri na AKTIVNIM uzorcima — odbačeni delovi su šum i ne smeju da kvare
    # ocenu onoga što je stvarno analizirano (kad nema odbačenih delova, sve ostaje isto).
    if dropped_s > 0.5 and int(keep_mask.sum()) > 1000:
        sig_q, clean_q = sig_raw[keep_mask], clean[keep_mask]
    else:
        sig_q, clean_q = sig_raw, clean
    qual = signal_quality(sig_q, clean_q, fs_used, rr_info, waves, rp,
                          dropped_pct=100 * dropped_s / max(1e-6, duration))
    sampling = {
        "nominal_hz": fs_nominal,
        "measured_hz": fs_measured,
        "used_hz": fs_used,
        "mismatch_pct": mismatch,
        "samples": int(n),
        "duration_s": duration,
        "time_axis": "timestamp" if fs_measured else "indeks/fs",
        "note": meta["timestamp_note"],
        "dropped_s": round(dropped_s, 1),
        "dropped_pct": round(100 * dropped_s / duration, 1) if duration else 0.0,
        "n_pauses": len(pauses),
        "n_gaps": len(gaps),
        "active_s": round(duration - dropped_s, 1),
        "samples_in_dropped": n_bad_samples,
        "measured_active_hz": round(fs_active, 1) if fs_active else None,
    }
    signal_stats = {
        "min": float(np.min(sig_raw)), "max": float(np.max(sig_raw)),
        "mean": float(np.mean(sig_raw)), "std": float(np.std(sig_raw)),
        "filtered_std": float(np.std(clean)),
    }

    # --- nezavisna kontrola HR: periodičnost signala (autokorelacija; izračunata pre detekcije)
    hr["spurious_removed"] = int(n_double)
    hr["periodicity_ok"] = bool(ac and ac.get("ok"))
    hr["correction_reverted"] = bool(ac_reverted)
    hr["independent_hr"] = round(hr_ind, 1) if hr_ind else None
    hr["dropped_s"] = round(dropped_s, 1)
    # Referenca za kontrolu: POUZDANA autokorelacija ILI nezavisni brojac (anvelopa).
    # Brojanje se prihvata ako se slaze sa BAR JEDNOM od njih (±15 %) — autokorelacija ume da
    # uhvati 2x harmonic i sa velikom korelacijom (16.10.2026: peak_corr 0.70 na 144 bpm dok je
    # puls bio 75), a nezavisni brojac ume da prebroji T talas; ako se bar jedan slaze sa
    # brojanjem, puls je verodostojan. Ako se brojanje ne slaze ni sa jednim — nepouzdano je.
    ac_ok = bool(ac and ac.get("ok") and ac.get("peak_corr", 0.0) >= AC_MIN_PEAK_CORR)

    def _agree(a, b):
        return bool(a and b and abs(a - b) / max(b, 1e-6) <= 0.15)

    ref_hr, ref_name, ref_note = None, None, None
    hr_rr_now = float(hr["median"]) if hr.get("median") else None
    if hr_rr_now:
        if ac_ok and _agree(hr_rr_now, float(ac["hr_bpm"])):
            ref_hr, ref_name = float(ac["hr_bpm"]), "autokorelacija"
        elif hr_ind and _agree(hr_rr_now, hr_ind):
            ref_hr, ref_name = float(hr_ind), "nezavisni brojač"
            if ac_ok:
                ref_note = (f"autokorelacija ({ac['hr_bpm']:.0f} bpm) se ne slaže sa brojanjem — "
                            "korišćena je nezavisna procena")
    if ref_hr and hr.get("median"):
        hr_rr = float(hr["median"])
        diff = abs(hr_rr - ref_hr) / max(ref_hr, 1e-6) * 100.0
        hr["hr_detection"] = round(hr_rr, 1)
        hr["hr_autocorr"] = round(float(ac["hr_bpm"]), 1) if ac else None
        hr["hr_reference"] = round(ref_hr, 1)
        hr["reference_source"] = ref_name
        hr["cross_check_diff_pct"] = round(diff, 1)
        hr["cross_check_ok"] = bool(diff <= 15.0)
        hr["periodicity_corr"] = round(ac["peak_corr"], 3) if ac else None
        if ref_note:
            hr["reference_note"] = ref_note
        log(f"    HR: brojanje {hr_rr:.0f} bpm, {ref_name} {ref_hr:.0f} bpm "
            f"(razlika {diff:.0f}% — {'slažu se' if hr['cross_check_ok'] else 'NE slažu se'})")
        if ref_note:
            log(f"    ! {ref_note}")

    result = {
        "generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "input": os.path.basename(input_path),
        "sampling": sampling,
        "signal": signal_stats,
        "heart": hr,
        "hrv": {"time": hrv_t, "freq": hrv_f},
        "intervals": iv,
        "median_beat": mb_iv,
        "consensus": consensus,
        "extra": extra,
        "quality": qual,
    }
    result["interpretations"] = interpret(hr, iv, hrv_t, hrv_f, qual, sampling, mb_iv, extra,
                                          consensus=consensus)
    # medicinsko tumačenje PRE laičkog: laički opis stanja čita anomalies iz medicinske sekcije
    if HAS_MEDICAL:
        try:
            result["medical"] = medical_interpretation(result)
        except Exception as exc:
            log(f"  ! medicinsko tumačenje nije uspelo: {exc}")
    result["layman"] = layman(result)

    # --- PNG
    os.makedirs(outdir, exist_ok=True)
    p_report = os.path.join(outdir, f"{prefix}_report.png")
    p_iv = os.path.join(outdir, f"{prefix}_intervals.png")
    try:
        plot_report(sig_raw, t, clean, rp, rr_info, hrv_t, p_report)
        log(f"✓ {os.path.basename(p_report)}")
    except Exception as exc:
        result.setdefault("warnings", []).append(f"grafik izveštaja: {exc}")
        log(f"✗ grafik izveštaja: {exc}")
        traceback.print_exc()
    try:
        plot_intervals(sig_raw, t, rp, waves, iv, p_iv)
        log(f"✓ {os.path.basename(p_iv)}")
    except Exception as exc:
        result.setdefault("warnings", []).append(f"grafik intervala: {exc}")
        log(f"✗ grafik intervala: {exc}")
        traceback.print_exc()

    result["files"] = {
        "report_png": os.path.basename(p_report),
        "intervals_png": os.path.basename(p_iv),
        "report_txt": f"{prefix}.txt",
        "results_json": f"{prefix}.json",
    }
    return result


def archive_session(result: dict, input_path: str, outdir: str, prefix: str,
                    archive_dir: str, keep: int = 10) -> str | None:
    """Kopiraj snimak + REZULTATE u data/history/<YYYYmmdd-HHMMSS>/.

    Poziva se POSLE upisa .json/.txt fajlova (inače bi se kopirala stara verzija).
    """
    try:
        import shutil
        ts = result.get("session") or datetime.now().strftime("%Y%m%d-%H%M%S")
        d = os.path.join(archive_dir, ts)
        os.makedirs(d, exist_ok=True)
        for suffix in (".json", ".txt", "_report.png", "_intervals.png"):
            src = os.path.join(outdir, prefix + suffix)
            if os.path.exists(src):
                shutil.copy2(src, d)
        shutil.copy2(input_path, os.path.join(d, "data.csv"))
        old = sorted(e for e in os.listdir(archive_dir) if re.fullmatch(r"\d{8}-\d{6}", e))
        for name in old[:-keep]:
            shutil.rmtree(os.path.join(archive_dir, name), ignore_errors=True)
        return ts
    except Exception as exc:
        log(f"  ! arhiviranje sesije nije uspelo: {exc}")
        return None


def write_report_txt(result: dict, path: str) -> None:
    s, h, iv, q = result["sampling"], result["heart"], result["intervals"], result["quality"]
    hrv_t, hrv_f = result["hrv"]["time"], result["hrv"]["freq"]
    L = []
    A = L.append
    A("=" * 62)
    A("ECG IZVEŠTAJ")
    A("=" * 62)
    A(f"Vreme analize : {result['generated']}")
    A(f"Ulaz          : {result['input']}")
    A(f"Trajanje      : {s['duration_s']:.1f} s ({s['duration_s']/60:.1f} min)")
    A(f"Uzoraka       : {s['samples']:,}")
    if s["nominal_hz"]:
        A(f"fs nominalna  : {s['nominal_hz']:.0f} Hz")
    if s["measured_hz"]:
        _act = s.get("measured_active_hz")
        if (s.get("dropped_pct") or 0) > 1 and _act:
            A(f"fs izmerena   : {s['measured_hz']:.1f} Hz preko cele ose; {_act:.1f} Hz na aktivnom delu "
              f"({s['mismatch_pct']:.1f}% odstupanja na aktivnom delu)")
        else:
            A(f"fs izmerena   : {s['measured_hz']:.1f} Hz  ({s['mismatch_pct']:.1f}% odstupanja)")
    A(f"Vremenska osa : {s['time_axis']} — {s['note']}")
    A("")
    if result.get("layman"):
        A("-" * 62)
        A("0. JEDNOSTAVNO OBJAŠNJENJE (bez stručnih izraza)")
        A("-" * 62)
        for line in result["layman"]:
            A("  " + line)
        A("")
    A("-" * 62)
    A("1. FREKVENCIJA I RITAM")
    A("-" * 62)
    A(f"  HR srednja          : {h['mean']:.1f} bpm")
    A(f"  HR medijana         : {h['median']:.1f} bpm   (najrobusnija vrednost)")
    A(f"  Broj beatova        : {h['beats']}")
    A(f"  Odbačeni RR (artef.) : {h['rr_artifacts']}")
    if h.get("pauses_over_2s"):
        A(f"  Pauze > 2 s         : {h['pauses_over_2s']}")
    A("")
    A("-" * 62)
    A("2. INTERVALI (medijana; IQR u zagradi)")
    A("-" * 62)
    mb_r = result.get("median_beat") or {}
    if mb_r.get("qrs_ms"):
        _pt = mb_r.get("p") or {}
        _tt = mb_r.get("t") or {}
        _pr = _pt.get("pr_ms")
        A(f"  MEDIJALNI BEAT : QRS {mb_r['qrs_ms']:.0f} ms"
          + (f", PR {_pr:.0f} ms" if _pr is not None and np.isfinite(_pr) else ", PR nije pouzdan")
          + (f", QT {_tt['qt_ms']:.0f} ms" if _tt.get("qt_ms") else ", QT nije pouzdan")
          + "   ← pouzdanije merenje (opseg 0.5–75 Hz, usrednjeno)")
        A("  (beat-po-beat merenja ispod; dwt ume da obuhvati i okolne talase — vrednosti QRS/QT/QTc")
        A("   iz tih redova su SIROVA merenja pojedinačnih udara i ne tumače se; merodavno je gore + sekcija 6)")
    for key, name, unit in (("pr", "PR", "ms"), ("qrs", "QRS", "ms"), ("qt", "QT", "ms"),
                            ("qtc_bazett", "QTc (Bazett)", "ms"), ("qtc_fridericia", "QTc (Fridericia)", "ms")):
        st = iv[key]
        if st.get("n"):
            lo, hi = st["iqr"]
            extra = f", odbačeno {st.get('n_rejected', 0)}" if st.get("n_rejected") else ""
            A(f"  {name:<16}: {st['median']*1000:7.0f} {unit}  (IQR {lo*1000:.0f}–{hi*1000:.0f}, n={st['n']}{extra})")
        else:
            A(f"  {name:<16}: nije izmeren")
    A(f"  Metod delineacije: {iv['delineation_method']}")
    A("")
    A("-" * 62)
    A("3. HRV")
    A("-" * 62)
    if hrv_t:
        A(f"  SDNN   : {hrv_t.get('sdnn_ms', float('nan')):.1f} ms")
        A(f"  RMSSD  : {hrv_t.get('rmssd_ms', float('nan')):.1f} ms")
        A(f"  pNN50  : {hrv_t.get('pnn50_pct', float('nan')):.1f} %")
        A(f"  SD1/SD2: {hrv_t.get('sd1_ms', 0):.1f} / {hrv_t.get('sd2_ms', 0):.1f} ms")
        A(f"  CV     : {hrv_t.get('cv_pct', float('nan')):.1f} %")
    if hrv_f:
        if hrv_f.get("lf_hf"):
            A(f"  LF/HF  : {hrv_f['lf_hf']:.2f}  (LF {hrv_f['lf_ms2']:.0f} ms², HF {hrv_f['hf_ms2']:.0f} ms²)"
              " — odnos snaga, nije mera autonomne ravnoteže"
              + ("; snimak kraći od 5 min → orijentaciono"
                 if float(hrv_f.get("duration_s") or 0) < 300 else ""))
            A(f"  LFnu/HFnu: {hrv_f.get('lf_nu', 0):.1f} / {hrv_f.get('hf_nu', 0):.1f}")
        if hrv_f.get("note"):
            A(f"  napomena: {hrv_f['note']}")
    A("")
    A("-" * 62)
    A(f"4. KVALITET SIGNALA: {q['label']} ({q['score']}/100)")
    A("-" * 62)
    A(f"  Saturacija ADC-a     : {q['clipped_pct']:.1f} % uzoraka")
    A(f"  HF šum (>40 Hz)      : {q['hf_noise_pct']:.1f} % snage")
    A(f"  Smetnja 50 Hz        : {q['powerline_pct']:.1f} % snage")
    A(f"  Baseline wander      : {q['baseline_wander_pct']:.1f} % snage <0.5 Hz")
    A(f"  RR artefakti         : {q['rr_artifact_pct']:.1f} %")
    A(f"  Uspešna delineacija  : {q['delineation_pct']:.0f} % beatova")
    if (s.get("dropped_pct") or 0) > 1:
        A(f"  Odbačeno u aplikaciji: {s['dropped_s']:.0f} s ({s['dropped_pct']:.0f}%) — "
          f"{s.get('n_pauses', 0)} pauza + {s.get('n_gaps', 0)} prekida toka (NISU srčane pauze)")
        _hz = s.get("measured_active_hz")
        A(f"  Aktivno analizirano  : {s.get('active_s', 0):.0f} s; fs na aktivnom delu "
          + (f"{_hz:.1f} Hz" if _hz else "nepoznata"))
    for note in q["notes"]:
        A(f"  ! {note}")
    A("")
    A("-" * 62)
    A("5. TUMAČENJE")
    A("-" * 62)
    n_item = 0
    for line in result["interpretations"]:
        if line.startswith("  "):
            A(line)
        else:
            n_item += 1
            A(f"  {n_item}. {line}")
    # --- 6. medicinsko tumačenje (stručno) -------------------------------------
    med_r = result.get("medical") or {}
    if med_r:
        A("")
        A("-" * 62)
        A("6. MEDICINSKO TUMAČENJE (normalne vrednosti i uslovi pouzdanosti)")
        A("-" * 62)
        for line in med_r.get("lines", []):
            A("  " + line)
        A("")
        A("  Zaključak: " + med_r.get("verdict", "–"))
        if med_r.get("anomalies"):
            A("")
            A("  Odstupanja (anomalije):")
            for a in med_r["anomalies"]:
                A("    ! " + a)
        else:
            A("")
            A("  Odstupanja: nema izmerenih odstupanja od normalnih vrednosti.")
        A("")
        A("  Uslovi pouzdanosti merenja:")
        for ch in med_r.get("checks", []):
            A("    - " + ch)
        A("")
        A("  Šta se jednokanalnim snimkom NE MOŽE utvrditi:")
        for c in med_r.get("cannot", []):
            A("    × " + c)
        A("")
        A("  Preporuke:")
        for s in med_r.get("next_steps", []):
            A("    → " + s)

    # --- 7. dodatne analize iz snimka ------------------------------------------
    ex = result.get("extra") or {}
    mb_i = result.get("median_beat") or {}
    if ex or mb_i:
        A("")
        A("-" * 62)
        A("7. DODATNE ANALIZE IZ OVOG SNIMKA")
        A("-" * 62)
        if mb_i.get("qrs_ms"):
            A(f"  Medijalni beat (usrednjen od {mb_i.get('n_beats', '?')} udara, međusobna sličnost "
              f"{mb_i.get('template_corr', '?')}):")
            A(f"    QRS  {mb_i['qrs_ms']:.0f} ms (onset {mb_i.get('qrs_on_ms', 0):+.0f} / offset "
              f"{mb_i.get('qrs_off_ms', 0):+.0f} ms oko R)")
            pt = mb_i.get("p") or {}
            if pt:
                pr_v = pt.get("pr_ms")
                A(f"    P    vrh {pt.get('peak_ms', 0):+.0f} ms, amplituda {pt.get('amp', 0):.0f} (SNR "
                  f"{pt.get('snr', 0):.1f}), PR " + (f"{pr_v:.0f} ms" if pr_v is not None and np.isfinite(pr_v) else "nije pouzdan"))
            tt = mb_i.get("t") or {}
            if tt:
                qt_v = tt.get("qt_ms")
                A(f"    T    vrh {tt.get('peak_ms', 0):+.0f} ms, kraj {tt.get('end_ms', 0):+.0f} ms, amplituda "
                  f"{tt.get('amp', 0):.0f} (SNR {tt.get('snr', 0):.1f}) → QT "
                  + (f"{qt_v:.0f} ms" if qt_v else "nije pouzdano izmeren"))
        ec = ex.get("ectopics") or {}
        # --- konsenzus merenja preko particija udara i segmenata snimka
        _cs = result.get("consensus") or {}
        if _cs.get("pr") or _cs.get("qrs"):
            A("")
            _hd = f"{_cs.get('k', 0)} particije udara"
            if _cs.get("n_segments"):
                _hd += f" + {_cs.get('n_segments')} segmenata od {(_cs.get('seg_s') or 0):.0f} s"
            A(f"  Konsenzus merenja ({_hd}):")
            for _k, _nm in (("pr", "PR"), ("qrs", "QRS"), ("qt", "QT")):
                _c = _cs.get(_k) or {}
                if not _c:
                    continue
                if not _c.get("n_ok"):
                    A(f"    {_nm}: nema upotrebljivog merenja ({_c.get('reason')})")
                elif _c.get("ok"):
                    A(f"    {_nm}: {_c['median']:.0f} ms — slaže se u {_c['n_ok']}/{_c['n_total']} grupa "
                      f"(raspon {_c['min_ms']:.0f}–{_c['max_ms']:.0f} ms, IQR {_c['iqr_ms']:.0f}) → pouzdano")
                else:
                    A(f"    {_nm}: NE tumači se — {_c.get('reason')} "
                      f"({_c.get('n_ok')}/{_c.get('n_total')} grupa, {_c.get('min_ms'):.0f}–{_c.get('max_ms'):.0f} ms)")
            if _cs.get("state_change"):
                A(f"    ⚠ promena stanja u snimku: puls po segmentima se menja {_cs.get('hr_range_pct')} % — "
                  "merenje sa celog snimka meša dve morfologije")
            _rows = _cs.get("segment_rows") or []
            if _rows:
                A(f"  Po segmentima ({(_cs.get('seg_s') or 0):.0f} s):")
                A(f"    {'t (s)':>11} {'ud':>4} {'HR':>5} {'PR':>6} {'QRS':>6} {'QT':>6} {'P-SNR':>7} {'slič.':>6}")
                for r in _rows:
                    def _f(v, nd=0):
                        return "   –" if v is None else f"{v:.{nd}f}"
                    A(f"    {r.get('t0', 0):>5.0f}–{r.get('t1', 0):<5.0f} {r.get('n', 0):>4}"
                      f" {_f(r.get('hr')):>5} {_f(r.get('pr_ms')):>6} {_f(r.get('qrs_ms')):>6}"
                      f" {_f(r.get('qt_ms')):>6} {_f(r.get('p_snr'), 1):>7} {_f(r.get('corr'), 2):>6}")
        if ec:
            A(f"  Ritam: RR {ec.get('rr_range_ms', [0, 0])[0]:.0f}–{ec.get('rr_range_ms', [0, 0])[1]:.0f} ms "
              f"(medijana {ec.get('median_rr_ms', 0):.0f} ms), preuranjenih udara {ec.get('premature', 0)}, "
              f"pauza {ec.get('pauses', 0)}")
        rsp = ex.get("respiration") or {}
        if rsp:
            A(f"  Disanje: {rsp.get('breaths_per_min')}/min (dominacija {rsp.get('dominance_pct')}%, "
              f"amplituda {rsp.get('amp', 0):.0f})")
        nl = ex.get("nonlinear") or {}
        if nl:
            A(f"  Nelinearna HRV: DFA α1 {nl.get('dfa_alpha1')}, SampEn {nl.get('sample_entropy')}"
              + ("  (orijentaciono — snimak < 5 min)" if nl.get("orientational") else ""))
        st = ex.get("st_trend") or {}
        if st.get("range_pct_r"):
            rng = st["range_pct_r"]
            trend = (f"trend {st.get('slope_pct_per_min', 0):+.1f} %R/min"
                     if st.get("trend_ok")
                     else f"trend se ne ocenjuje (samo {st.get('n_segments', 0)} segment(a) — snimak je prekratak)")
            A(f"  ST (relativno, J+60 ms): {rng[0]}…{rng[1]} %R, " + trend
              + ("  ← vredno pažnje" if st.get("significant")
                 else ("  (bez značajne promene)" if st.get("trend_ok") else "")))
            A("    (nekalibrisan snimak — ovo je trend unutar snimka, ne klinički ST nalaz)")
        nt = ex.get("noise_timeline") or []
        if nt:
            worst = max(nt, key=lambda s: s["hf_pct"])
            A(f"  Mapa šuma: najgori segment {worst['t0']:.0f}–{worst['t1']:.0f} s (HF šum {worst['hf_pct']}%), "
              f"prosečno {np.mean([s['hf_pct'] for s in nt]):.1f}%")
            clean_segs = [f"{s['t0']:.0f}–{s['t1']:.0f}s" for s in nt if s["hf_pct"] <= 5]
            if clean_segs:
                A(f"    Najčistiji delovi: {', '.join(clean_segs)}")
        A("")
        A("  Analize koje zahtevaju drugačiji snimak:")
        A("    × standardna HRV po Task Force (1996) — potreban miran snimak od 5 min")
        A("    × osa srca, hipertrofija, infarkt, tip bloka grane — potrebno 2+ odvoda (12-kanalni ECG)")
        A("    × ST u mV / ishemija — potrebna kalibracija u mV i više odvoda")

    if result.get("warnings"):
        A("")
        A("Greške pri crtanju:")
        for w in result["warnings"]:
            A(f"  - {w}")
    A("")
    A("=" * 62)
    A(DISCLAIMER)
    A("=" * 62)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="ECG analiza")
    ap.add_argument("--input", default="data/data.csv")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--prefix", default="data")
    ap.add_argument("--fs", type=float, default=None, help="nominalna frekvencija uzorkovanja [Hz]")
    ap.add_argument("--job-json", default=None, help="fajl u koji se upisuje status posla")
    ap.add_argument("--archive-dir", default=None, help="direktorijum za istoriju snimaka")
    ap.add_argument("--source", default="cli", help="serial | upload | csv | cli")
    args = ap.parse_args()

    # status posla se dopunjava, a ne prepisuje (PHP je upisao id/fs pre pokretanja)
    job: dict = {}
    if args.job_json and os.path.exists(args.job_json):
        try:
            with open(args.job_json, encoding="utf-8") as fh:
                job = json.load(fh) or {}
        except (OSError, ValueError):
            job = {}
    job.update({"state": "running", "started": datetime.now().isoformat(timespec="seconds"),
                "started_ts": int(datetime.now().timestamp()), "source": args.source, "fs": args.fs})
    if args.job_json:
        try:
            with open(args.job_json, "w", encoding="utf-8") as fh:
                json.dump(job, fh, ensure_ascii=False)
        except OSError:
            pass

    try:
        result = analyse(args.input, args.outdir, args.prefix, args.fs)
        result["source"] = args.source
        result["disclaimer"] = DISCLAIMER
        os.makedirs(args.outdir, exist_ok=True)
        if args.archive_dir:
            os.makedirs(args.archive_dir, exist_ok=True)
            result["session"] = datetime.now().strftime("%Y%m%d-%H%M%S")
        txt = os.path.join(args.outdir, f"{args.prefix}.txt")
        jsn = os.path.join(args.outdir, f"{args.prefix}.json")
        write_report_txt(result, txt)
        with open(jsn, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        try:  # stari naziv fajla (kompatibilnost sa ranijim stranicama/linkovima)
            import shutil
            shutil.copy2(txt, os.path.join(args.outdir, "report.txt"))
        except OSError:
            pass
        if args.archive_dir:
            archive_session(result, args.input, args.outdir, args.prefix, args.archive_dir)
            log(f"✓ sesija {result['session']} arhivirana")
        log(f"✓ {os.path.basename(txt)} i {os.path.basename(jsn)}")
        if args.job_json:
            job.update({"state": "done", "finished": datetime.now().isoformat(timespec="seconds"),
                        "result": jsn, "report": txt, "heart": result["heart"],
                        "quality": result["quality"], "session": result.get("session")})
            with open(args.job_json, "w", encoding="utf-8") as fh:
                json.dump(job, fh, ensure_ascii=False)
        return 0
    except Exception as exc:
        log(f"✗ GREŠKA: {exc}")
        traceback.print_exc()
        if args.job_json:
            job.update({"state": "failed", "finished": datetime.now().isoformat(timespec="seconds"),
                        "error": str(exc)})
            try:
                with open(args.job_json, "w", encoding="utf-8") as fh:
                    json.dump(job, fh, ensure_ascii=False)
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
