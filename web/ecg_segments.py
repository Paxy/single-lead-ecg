"""Konsenzus merenja preko segmenata snimka i particija udara.

Zašto: jedno merenje sa celog snimka (jedan medijalni beat) je JEDNA procena i iz nje se
ne vidi da li je stabilna. Ovde se isti medijalni beat pravi više puta:

  * particije — udari se podele na K uzastopnih grupa (radi i na snimku od 60 s),
  * segmenti  — snimak se podeli na prozore od `seg_s` sekundi (5–30 min snimci;
                ujedno pokazuje i promenu stanja, npr. 60 → 100 bpm).

Konsenzus je TEST RASIpa, ne prosek: parametar se tumači samo ako se većina grupa slaže
(IQR ≤ max_iqr i raspon ≤ 2×max_iqr). Ako se ne slažu — broj se NE tumači, a raspon se
ispisuje (to je dijagnostika: "PR 100–224 ms po segmentima" odmah pokaže da merenje
P onseta nije stabilno).

⚠ Sistemsku grešku (npr. baza P talasa uzeta sa drifta 0.5 Hz filtra) konsenzus NE
otkriva — sve grupe bi se složile na istoj pogrešnoj vrednosti. Zato baza/merenje mora
biti ispravno PRE ovoga (vidi `intervals_from_median_beat` u ecg_medical.py).
"""
from __future__ import annotations

import numpy as np

try:
    from ecg_medical import median_beat, intervals_from_median_beat
except Exception:                                                # pragma: no cover
    median_beat = intervals_from_median_beat = None

# pragovi konsenzusa (ms)
MAX_IQR = {"pr": 20.0, "qrs": 15.0, "qt": 25.0}
MIN_VAL = {"pr": 70.0, "qrs": 40.0, "qt": 200.0}
MAX_VAL = {"pr": 320.0, "qrs": 220.0, "qt": 640.0}
MIN_CORR = 0.95          # sličnost otkucaja unutar grupe
MIN_FRAC = 0.7           # udeo grupa koje moraju proći


def _row(wide, clean, rp_sub, fs, t=None):
    """Medijalni beat jedne grupe udara → PR/QRS/QT + pokazatelji pouzdanosti."""
    if median_beat is None or len(rp_sub) < 5:
        return None
    mb = median_beat(wide, clean, np.asarray(rp_sub), fs)
    if not mb:
        return None
    iv = intervals_from_median_beat(mb)
    if not iv:
        return None
    p = iv.get("p") or {}
    tt = iv.get("t") or {}
    row = {
        "n": int(mb["n_beats"]),
        "corr": float(mb["template_corr_median"]),
        "qrs_ms": iv.get("qrs_ms"),
        "qrs_on_ms": iv.get("qrs_on_ms"),
        "pr_ms": p.get("pr_ms"),
        "p_peak_ms": p.get("peak_ms"),
        "p_amp": p.get("amp"),
        "p_snr": p.get("snr"),
        "p_width_ms": (p.get("off_ms") - p.get("on_ms")) if p.get("on_ms") is not None and p.get("off_ms") is not None else None,
        "p_present": bool(p.get("present")),
        "qt_ms": tt.get("qt_ms") if tt.get("reliable") else None,
    }
    for k, v in list(row.items()):
        if isinstance(v, float) and not np.isfinite(v):
            row[k] = None
    if t is not None and len(rp_sub) > 1:
        rr = np.diff(np.asarray(t)[np.asarray(rp_sub)])
        rr = rr[(rr >= 0.3) & (rr <= 2.0)]
        if rr.size >= 3:
            row["hr"] = float(60.0 / np.median(rr))
            row["rr_cv_pct"] = float(np.std(rr) / np.median(rr) * 100.0)
    return row


def _valid(row, key):
    field = {"pr": "pr_ms", "qrs": "qrs_ms", "qt": "qt_ms"}[key]
    if not row or row.get("corr", 0) < MIN_CORR:
        return False
    v = row.get(field)
    if v is None or not np.isfinite(v):
        return False
    if not (MIN_VAL[key] <= float(v) <= MAX_VAL[key]):
        return False
    if key == "pr":
        w = row.get("p_width_ms")
        # P talas širi od 120 ms nije čist P talas — to je drift ili spoj sa T/U talasom
        if not row.get("p_present") or w is None or not (35.0 <= float(w) <= 120.0):
            return False
        # Na visokoj frekvenciji prethodni T ulazi u prozor P talasa (P-on-T), pa se PR
        # jednokanalnim snimkom ne meri — bolje blokirati nego prijaviti lažni AV blok.
        if row.get("hr") and float(row["hr"]) > 105.0:
            return False
    return True


def _consensus(rows, key, min_frac=MIN_FRAC, max_iqr=None, label=""):
    """Konsenzus jednog parametra preko grupa: median + IQR + raspon + da/ne odluka."""
    field = {"pr": "pr_ms", "qrs": "qrs_ms", "qt": "qt_ms"}[key]
    max_iqr = MAX_IQR[key] if max_iqr is None else max_iqr
    good = [r for r in rows if _valid(r, key)]
    vals = [float(r[field]) for r in good]
    total = len([r for r in rows if r])
    out = {"n_total": total, "n_ok": len(vals), "min_iqr_ms": max_iqr, "label": label}
    if not vals:
        out.update({"median": None, "iqr_ms": None, "min_ms": None, "max_ms": None,
                    "ok": False, "reason": "nijedna grupa nije dala upotrebljivo merenje"})
        return out
    vals_sorted = sorted(vals)
    med = float(np.median(vals_sorted))
    p25, p75 = np.percentile(vals_sorted, [25, 75])
    iqr = float(p75 - p25)
    rng = float(vals_sorted[-1] - vals_sorted[0])
    # Pravilo broja grupa: apsolutni minimum (3, ili sve ako ih je manje) I pokrivenost
    # (bar pola grupa) — osim kada je puno grupa neupotrebljivo zbog šuma (dugačak snimak sa
    # pauzama): tada je 10 grupa koje se slažu jak dokaz i bez 50 % pokrivenosti.
    n_min = 3 if total >= 3 else max(total, 1)
    ok = (len(vals) >= n_min) and (len(vals) >= 0.5 * total or len(vals) >= 10) \
        and (iqr <= max_iqr) and (rng <= 2.0 * max_iqr)
    if len(vals) < n_min:
        reason = f"premalo upotrebljivih grupa ({len(vals)}/{total}, potrebno {n_min})"
    elif len(vals) < 0.5 * total and len(vals) < 10:
        reason = (f"premalo upotrebljivih grupa ({len(vals)}/{total}) — ostatak je bio preslab "
                  f"za merenje")
    elif iqr > max_iqr or rng > 2.0 * max_iqr:
        reason = (f"grupe se ne slažu (raspon {rng:.0f} ms, IQR {iqr:.0f} ms — "
                  f"dozvoljeno IQR {max_iqr:.0f})")
    else:
        reason = "grupe se slažu"
    out.update({"median": round(med, 1), "iqr_ms": round(iqr, 1),
                "min_ms": round(vals_sorted[0], 1), "max_ms": round(vals_sorted[-1], 1),
                "ok": bool(ok), "reason": reason, "values": [round(v, 1) for v in vals_sorted]})
    return out


def partition_consensus(wide, clean, rp, fs, t=None, k=4, min_beats=15):
    """Podeli udare na k uzastopnih grupa i traži saglasnost merenja."""
    rp = np.asarray(rp)
    if rp.size < min_beats * 2:
        _short = "premalo udara za particije (min. 2 × 15)"
        return {"k": 0, "pr": {"ok": False, "reason": _short},
                "qrs": {"ok": False, "reason": _short}, "qt": {"ok": False, "reason": _short},
                "rows": []}
    k = int(min(k, max(2, rp.size // min_beats)))
    rows, idx = [], np.array_split(np.arange(rp.size), k)
    for part in idx:
        if part.size < min_beats:
            rows.append(None)
            continue
        r = _row(wide, clean, rp[part], fs, t)
        if r:
            r["from_beat"] = int(part[0]); r["to_beat"] = int(part[-1])
        rows.append(r)
    out = {"k": k, "min_beats": min_beats, "rows": [r for r in rows if r]}
    for key, lab in (("pr", "particije udara"), ("qrs", "particije udara"), ("qt", "particije udara")):
        out[key] = _consensus(rows, key, label=lab)
    return out


def segment_rows(wide, clean, rp, t, fs, seg_s=30.0, min_beats=15, dropped=()):
    """Po segmentu od `seg_s` sekundi: isti medijalni beat + HR/RR rasipanje."""
    rp = np.asarray(rp)
    t = np.asarray(t)
    if rp.size < min_beats:
        return {"seg_s": seg_s, "rows": []}
    t0, t1 = float(t[rp[0]]), float(t[rp[-1]])
    rows = []
    a = t0
    while a < t1:
        b = min(a + seg_s, t1 + 1e-9)
        sel = rp[(t[rp] >= a) & (t[rp] < b)]
        if len(sel) >= min_beats:
            # preskoči segmente koji su većinom u odbačenim (pauza/šum) delovima
            bad = 0.0
            for (da, db, *_rest) in (dropped or ()):
                bad += max(0.0, min(b, float(db)) - max(a, float(da)))
            if bad < 0.5 * (b - a):
                r = _row(wide, clean, sel, fs, t)
                if r:
                    r["t0"] = round(a - t0, 1)
                    r["t1"] = round(b - t0, 1)
                    rows.append(r)
        a = b
    return {"seg_s": seg_s, "rows": rows}


def analyse(wide, clean, rp, t, fs, seg_s=30.0, k=4, min_beats=15, dropped=()):
    """Particije + segmenti + konsenzus. Vraća strukturu koja ide u JSON (`result["consensus"]`)."""
    part = partition_consensus(wide, clean, rp, fs, t=t, k=k, min_beats=min_beats)
    t = np.asarray(t)
    dur = float(t[rp[-1]] - t[rp[0]]) if len(rp) > 1 else 0.0
    segs = {"seg_s": seg_s, "rows": []}
    if dur >= max(3 * seg_s, 90.0):
        segs = segment_rows(wide, clean, rp, t, fs, seg_s=seg_s, min_beats=min_beats, dropped=dropped)
    out = {"k": part.get("k", 0), "seg_s": segs.get("seg_s"), "min_beats": min_beats,
           "n_segments": len(segs.get("rows") or []), "rows": part.get("rows") or [],
           "segment_rows": segs.get("rows") or []}
    for key in ("pr", "qrs", "qt"):
        c = dict(part.get(key) or {})
        c["source"] = "particije"
        #: konsenzus preko segmenata je jači dokaz (više grupa, pokriva i promenu stanja).
        #: Ako segmenti daju bar 3 upotrebljiva merenja, njihov verdikt zamenjuje particije —
        #: i kada je "ne slažu se", jer je to razlog koji treba da se vidi u nalazu.
        if len(segs.get("rows") or []) >= 3:
            cs = _consensus(segs["rows"], key, label="segmenti")
            if cs.get("n_ok", 0) >= 3:
                cs["source"] = "segmenti"
                c = cs
        out[key] = c
    #: promena stanja — ako HR po segmentima šeta više od 20 %, merenja sa celog snimka
    #: mešaju dve morfologije i to mora da se vidi u nalazu
    hrs = [r["hr"] for r in (segs.get("rows") or []) if r.get("hr")]
    if len(hrs) >= 3:
        med_hr = float(np.median(hrs))
        out["hr_range_pct"] = round(float((max(hrs) - min(hrs)) / max(med_hr, 1e-6) * 100.0), 1)
        out["state_change"] = bool(out["hr_range_pct"] > 20.0)
    return out
