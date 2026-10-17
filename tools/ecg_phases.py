#!/usr/bin/env python3
"""Razlaganje dugog ECG snimka (5-10 min) na faze — istim motorom kao servis.

Pokreći NA SERVERU, venv-om servisa (mora da vidi ecg_analyze.py):
  /opt/ecg-venv/bin/python3 ecg_phases.py /tmp/snimak.csv --seg 30
  /opt/ecg-venv/bin/python3 ecg_phases.py /tmp/snimak.csv --seg 60 --json /tmp/faze.json

Zašto: `report.sh`/`ecg_analyze.py` daje jedan nalaz za ceo snimak. Kad snimak traje
5-10 min i kroz njega se stanje menja (mirovanje -> ustajanje -> posle kupke/leka ->
smirivanje), treba videti PO FAZI: puls, pravilnost ritma (ektopije), HRV i šum.

VAŽNO: detekcija udara i HR nisu „moji" — pozajmljeni su iz `ecg_analyze.py`
(`detect_beats` = neurokit, `correct_double_counts`, `hr_autocorr`, `rr_analysis`,
`hrv_time`) i iz `ecg_medical.py` (`noise_timeline`, `ectopics_and_pauses`), da bi
puls po fazi bio identičan broju koji daje glavni izveštaj. Kućni detektor na |d/dt|
je odbačen: na stvarnom snimku (25.09., HR 67) nalazio je 36 udara/min i lažno
prijavljivao „nepravilan ritam".

Ne dira `data/` servisa — samo čita zadati CSV i štampa tabelu (opciono JSON).
"""
import argparse
import json
import os
import re
import sys

import numpy as np

SERVICE_DIR = os.environ.get('ECG_SERVICE_DIR', '/var/www/html/ecg')
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

import neurokit2 as nk            # noqa: E402
import ecg_analyze as EA          # noqa: E402
import ecg_medical as EM          # noqa: E402


def prepare(path, fs_nominal):
    """Isti put kao EA.analyse(): učitavanje, ose, čišćenje, vrhovi."""
    sig_raw, times, meta = EA.load_signal(path, fs_nominal)
    n = sig_raw.size
    if n < 500:
        raise SystemExit(f'premalo uzoraka ({n})')
    fs_fb = float(fs_nominal or 192.0)
    t, fs_meas = EA.build_time_axis(times, n, fs_fb)
    # Prekidi toka podataka (praznine u vremenskoj osi) — NISU srčane pauze, ali ih analiza
    # mora prepoznati: inače RR preko praznine izgleda kao „pauza u ritmu" (realan slučaj:
    # snimak od 15 min sa 71 s izgubljenih uzoraka → servis prijavio „RR 72012 ms").
    gaps = []
    if times is not None:
        tt = np.asarray(times, dtype=float)
        dt = np.diff(tt)
        for i in np.where(dt > 5.0)[0]:
            gaps.append((float(tt[i] - tt[0]), float(dt[i])))
    dur = float(t[-1] - t[0])
    fs_used = float(fs_nominal) if fs_nominal else (fs_meas or fs_fb)
    clean = nk.ecg_clean(sig_raw, sampling_rate=fs_used, method='neurokit')
    ac = EA.hr_autocorr(clean, fs_used)
    rp_raw = EA.detect_beats(clean, fs_used)
    rp, n_double, ac_rev = EA.correct_double_counts(
        rp_raw, clean, (ac['period_s'] * fs_used) if ac else None, trusted=bool(ac))
    if n_double and ac and len(rp) > 2:
        hr_after = float(np.median(60.0 / np.diff(t[rp])))
        if hr_after > 0 and abs(hr_after - ac['hr_bpm']) / ac['hr_bpm'] > 0.15:
            rp, n_double, ac_rev = rp_raw, 0, True
    return dict(sig_raw=sig_raw, clean=clean, t=t, fs=fs_used, fs_meas=fs_meas, dur=dur,
                rp=rp, rp_raw=rp_raw, n_double=n_double, ac_rev=ac_rev, ac=ac, meta=meta,
                gaps=gaps)


def seg_metrics(prep, a, b, min_beats):
    t, rp = prep['t'], prep['rp']
    sel = rp[(t[rp] >= a) & (t[rp] < b)]
    m = {'start_s': a, 'end_s': b, 'beats_in': int(len(sel))}
    if len(sel) < min_beats + 1:
        return m
    rr_all = np.diff(t[sel])
    phys = (rr_all >= 0.3) & (rr_all <= 2.0)
    clean_mask = phys.copy()
    # RR parovi koji prelaze prekid toka podataka nisu srčani interval — izbaci ih
    gap_s = 0.0
    if prep.get('gaps'):
        ts = t[sel]
        cross = np.zeros(rr_all.size, dtype=bool)
        for g0, gd in prep['gaps']:
            cross |= (ts[:-1] < g0) & (ts[1:] > g0 + gd * 0.5)
            gap_s += max(0.0, min(b, g0 + gd) - max(a, g0))
        clean_mask &= ~cross
    if phys.sum() >= 8:
        lr = np.log(rr_all[phys])
        med_lr = np.median(lr)
        mad = np.median(np.abs(lr - med_lr)) or 1e-6
        clean_mask[phys] = np.abs(lr - med_lr) / (1.4826 * mad) <= 3.5
    rr = rr_all[clean_mask]
    if rr.size < 3:
        return m
    med = float(np.median(rr))
    p25, p75 = np.percentile(rr, [25, 75])
    hrv = EA.hrv_time(rr)
    ect = EM.ectopics_and_pauses(rr)
    m.update(
        hr=60.0 / med,            # rr je u SEKUNDAMA (hrv_time sam prebacuje u ms)
        rr_med_ms=med * 1000,
        rr_min_ms=float(rr.min()) * 1000,
        rr_max_ms=float(rr.max()) * 1000,
        sdnn_ms=hrv.get('sdnn_ms'),
        rmssd_ms=hrv.get('rmssd_ms'),
        irr_pct=100.0 * (p75 - p25) / med,
        premature_pct=100.0 * ect['premature'] / max(len(rr), 1),
        premature=ect['premature'],
        pauses=ect['pauses'],
        dropped=int((~clean_mask).sum()),
        gap_s=gap_s,
    )
    return m


def classify(m, global_hr):
    if m.get('hr') is None:
        return 'prekid toka — nije snimano' if m.get('gap_s', 0) >= 3 else 'premalo udara/šum'
    tags = []
    if m.get('gap_s', 0) >= 3:
        tags.append(f'prekid toka {m["gap_s"]:.0f} s')
    if m['pauses']:
        tags.append(f"pauze: {m['pauses']}")
    if m['premature'] and m['premature_pct'] > 10:
        tags.append(f"ektopije {m['premature_pct']:.0f}%")
    elif m['irr_pct'] > 25:
        tags.append(f"RR varijabilan (IQR {m['irr_pct']:.0f}%)")
    if global_hr and abs(m['hr'] - global_hr) / global_hr > 0.20:
        tags.append('promena faze (>20% od proseka)')
    return '; '.join(tags) if tags else 'OK'


MARK_RE = re.compile(r'^#\s*MARK\s+t=(\d+):(\d+(?:\.\d+)?)\s+label=(.*)$')


def read_markers(path):
    """Markeri koje upisuje Android aplikacija: `# MARK t=03:24 label=posle kupke`."""
    out = []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not line.startswith('#'):
                    continue
                m = MARK_RE.match(line.strip())
                if m:
                    out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    except OSError:
        pass
    return out


EVENT_RE = re.compile(r'^#\s*(START|PAUSE|RESUME|STOP)\b[^\n]*?t=(\d+):(\d+(?:\.\d+)?)\s*(.*)$')


def read_events(path):
    """Kontrolne linije aplikacije: START/PAUSE/RESUME/STOP sa t=MM:SS i labelom.

    Automatska pauza (elektroda otpala ili sum) upisuje `# PAUSE ... label=...`.
    U aplikaciji do 2.0 u tom intervalu NEMA uzoraka, a od 2.1 uzorci postoje (sirovo,
    oznaceni) — u oba slucaja interval treba izuzeti iz analize: inace praznina izgleda
    kao artefakt, a sirovi sum daje lazni ritam.
    """
    out = []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not line.startswith('#'):
                    continue
                m = EVENT_RE.match(line.strip())
                if m:
                    t = int(m.group(2)) * 60 + float(m.group(3))
                    # Aplikacija 2.1+ ispred label-a pise i metrike (iso=, pp=, jump=, sat=,
                    # state=) — label je uvek POSLEDNJE polje, pa se cita regexom do kraja.
                    rest = m.group(4).strip()
                    lm = re.search(r'label=(.*)$', rest)
                    label = (lm.group(1) if lm else rest.replace('label=', '')).strip()
                    out.append((t, m.group(1), label))
    except OSError:
        pass
    return out


def pair_pauses(events, total_s):
    """(PAUSE -> RESUME) parovi kao intervali [t0, t1] u sekundama od početka snimka."""
    pauses, open_since = [], None
    for t, kind, label in events:
        if kind == 'PAUSE':
            open_since = (t, label)
        elif kind in ('RESUME', 'STOP') and open_since:
            pauses.append((open_since[0], t, open_since[1]))
            open_since = None
    if open_since:
        pauses.append((open_since[0], total_s, open_since[1]))
    return pauses


def pause_overlap(a, b, pauses):
    """Deo segmenta [a,b) koji je u pauzi (0..1)."""
    if not pauses or b <= a:
        return 0.0
    ov = 0.0
    for p0, p1, _ in pauses:
        ov += max(0.0, min(b, p1) - max(a, p0))
    return min(1.0, ov / (b - a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv')
    ap.add_argument('--seg', type=float, default=30.0, help='dužina segmenta u sekundama')
    ap.add_argument('--fs', type=float, default=192.0, help='nominalna fs ako CSV nema Timestamp')
    ap.add_argument('--min-beats', type=int, default=4)
    ap.add_argument('--json')
    a = ap.parse_args()

    markers = read_markers(a.csv)
    prep = prepare(a.csv, a.fs)
    pauses = pair_pauses(read_events(a.csv), prep['dur'])
    t, rp = prep['t'], prep['rp']
    mm = lambda s: f'{int(s) // 60}:{s - 60 * (int(s) // 60):04.1f}'
    rr_info = EA.rr_analysis(rp, t)
    hr_all = 60.0 / rr_info['rr_all'][rr_info['mask_clean']]
    global_hr = float(np.median(hr_all)) if hr_all.size else None
    noise = {round(x['t0']): x for x in EM.noise_timeline(prep['sig_raw'], t, prep['fs'], seg_s=a.seg)}

    print(f'snimak: {a.csv}')
    print(f'uzoraka {prep["sig_raw"].size}  trajanje {prep["dur"]:.1f}s ({prep["dur"] / 60:.1f} min)  '
          f'fs izmerena {prep["fs_meas"]:.2f} Hz  (korišćena {prep["fs"]:.0f} Hz)')
    line = f'R vrhova {len(rp)}'
    if prep['n_double']:
        line += f' (uklonjeno {prep["n_double"]} lažnih)'
    if prep['ac_rev']:
        line += '  [korekcija poništena]'
    if global_hr:
        line += f'   puls ceo snimak: {global_hr:.1f} bpm'
    print(line)
    if prep['ac'] and global_hr:
        print(f'kontrola periodičnosti: {prep["ac"]["hr_bpm"]:.1f} bpm (razlika '
              f'{abs(global_hr - prep["ac"]["hr_bpm"]) / prep["ac"]["hr_bpm"] * 100:.1f}%)')
    if prep['gaps']:
        print('PREKIDI TOKA PODATAKA (nisu srčane pauze): '
              + ', '.join(f'na {g / 60:.2f} min +{d:.0f} s' for g, d in prep['gaps']))
    print()
    print(f'{"faza(od)":>9} | {"puls":>5} | {"ud/min":>6} | {"SDNN":>5} | {"RMSSD":>6} | '
          f'{"IQR RR":>7} | {"ektopije":>8} | {"šum>40":>7} | ocena')
    rows, s0 = [], 0.0
    while s0 < prep['dur']:
        s1 = min(s0 + a.seg, prep['dur'])
        m = seg_metrics(prep, s0, s1, a.min_beats)
        nz = noise.get(round(s0))
        m['hf_pct'] = nz['hf_pct'] if nz else None
        m['bw_pct'] = nz['bw_pct'] if nz else None
        m['rating'] = classify(m, global_hr)
        if pause_overlap(s0, s1, pauses) > 0.5:
            m['rating'] = 'pauza — nije snimano'
        udmin = m['beats_in'] * 60 / max(s1 - s0, 1e-9)
        if m.get('hr') is not None:
            print(f'{mm(s0):>9} | {m["hr"]:5.1f} | {udmin:6.0f} | '
                  f'{(m["sdnn_ms"] or 0):5.0f} | {(m["rmssd_ms"] or 0):6.0f} | '
                  f'{m["irr_pct"]:6.0f}% | {m["premature_pct"]:7.0f}% | '
                  f'{(m["hf_pct"] or 0):6.0f}% | {m["rating"]}')
        else:
            print(f'{mm(s0):>9} | {"-":>5} | {udmin:6.0f} | {"-":>5} | {"-":>6} | '
                  f'{"-":>7} | {"-":>8} | {(m["hf_pct"] or 0):6.0f}% | premalo udara/šum')
        rows.append(m)
        s0 = s1

    faze = []
    for m in rows:
        if m.get('hr') is None:
            continue
        if str(m.get('rating', '')).startswith('pauza'):
            continue
        if faze and m['start_s'] == faze[-1]['end_s'] and abs(faze[-1]['hr'] - m['hr']) <= 5:
            g = faze[-1]
            g['hr'] = (g['hr'] * g['n'] + m['hr']) / (g['n'] + 1)
            g['n'] += 1
            g['end_s'] = m['end_s']
            g['ratings'].add(m['rating'])
        else:
            faze.append({'start_s': m['start_s'], 'end_s': m['end_s'], 'hr': m['hr'], 'n': 1,
                         'ratings': {m['rating']}})
    print()
    print('FAZE (uzastopni segmenti sa pulsom unutar +-5 bpm):')
    for g in faze:
        if g['end_s'] - g['start_s'] >= min(a.seg, 30):
            r = '; '.join(sorted(x for x in g['ratings'] if x != 'OK')) or 'OK'
            print(f'  {mm(g["start_s"])}-{mm(g["end_s"])}  ~{g["hr"]:.1f} bpm  ({g["n"]} seg)  {r}')
    if pauses:
        print()
        print('PAUZE (aplikacija je prestala da upisuje — npr. elektroda otpala):')
        for p0, p1, label in pauses:
            print(f'  {mm(p0)}-{mm(p1)}  ({p1 - p0:.0f} s)  {label}')
    if markers:
        print()
        print('MARKERI iz snimka (dugmad u aplikaciji):')
        for tsec, label in markers:
            row = next((m for m in rows if m['start_s'] <= tsec < m['end_s']), None)
            hr = f"  puls tu: {row['hr']:.1f} bpm" if (row and row.get('hr')) else ''
            print(f'  {mm(tsec)}  {label}{hr}')
    hrs = [m['hr'] for m in rows if m.get('hr') is not None]
    if hrs:
        print(f'\npuls po fazama: min {min(hrs):.1f} / max {max(hrs):.1f} bpm (raspon {max(hrs) - min(hrs):.1f})')
    if a.json:
        json.dump({'file': a.csv, 'fs': prep['fs'], 'dur_s': prep['dur'], 'global_hr': global_hr,
                   'segments': rows,
                   'phases': [{k: (sorted(v) if isinstance(v, set) else v) for k, v in g.items()} for g in faze]},
                  open(a.json, 'w'), ensure_ascii=False, default=float)
        print(f'JSON: {a.json}')


if __name__ == '__main__':
    main()
