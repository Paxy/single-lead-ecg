#!/usr/bin/env python3
"""Generator sintetičkih ECG snimaka za test analize bez hardvera.

  python3 gen_test_ecg.py regular 112 60 > /tmp/reg.csv   # sinusna tahikardija, HR 112, 60 s
  python3 gen_test_ecg.py af 60 > /tmp/af.csv             # nepravilan RR, bez P talasa
  python3 gen_test_ecg.py brady 48 60 > /tmp/b.csv        # bradikardija
  python3 gen_test_ecg.py app 75 360 > /tmp/app21.csv     # format aplikacije 2.1: markeri sa brojevima
                                                          # i uzorci UPISANI i u pauzi (data-contract test)

Format izlaza je isti kao izvoz `plotter.html`: `Timestamp,S1`, ADC opseg 0-1023, fs 192 Hz.
Namena: provera da li servis ispravno čita snimak VISOKOG pulsa (to je slučaj kod
„high heart rate at rest" alarma) i da li ume da prepozna nepravilan ritam.

Izmereno 16.10.2026 (servis na your-server):
- regular 112 -> HR detekcija 111.9, autokorelacija 112, cross_check ok, periodicnost 0.98,
  anomalija „Tahikardija 112/min" (ispravno).
- af -> „Ritam: nepravilan (RR 349-854 ms)", 16 preuranjenih udara, cross_check NIJE ok
  (frekvencija se ne tumači) — ali izveštaj pogrešno tvrdi „Sinusni ritam: verovatan,
  P talas nađen u 71% udara" iako P talasa nema -> tu liniju ne koristiti kao dokaz
  sinusnog ritma dok se P detekcija ne popravi.
"""
import argparse
import datetime
import math
import random


def gauss(x, mu, sigma):
    d = x - mu
    return math.exp(-(d * d) / (2 * sigma * sigma))


def beat_train(t, rr_list, idx, t_last):
    """Vrati (QRS doprinos) za dati trenutak t."""
    return 0.0


def gen_app(hr, dur, fs, seed):
    """CSV u formatu koji pise Android aplikacija 2.1 (data-contract test bez telefona).

    Kljucna razlika od ostalih kind-ova: uzorci se upisuju I UNUTAR pauza (sirovo), a pauze su
    oznacene markerima sa brojevima (`pp`, `jump`, `hf`, `sat`, `state`, `auto`) i `iso=` vremenom;
    `label=` je uvek POSLEDNJE polje. Prva pauza = sum (slucajni uzorci), druga = saturacija (sine).
    Motor mora da vrati tacan HR (pauze maskira preko markera) i da prijavi `dropped_pct`.
    """
    rnd = random.Random(seed)
    n = int(fs * dur)
    t0 = datetime.datetime(2026, 10, 16, 18, 0, 0)

    def iso(sec):
        return (t0 + datetime.timedelta(seconds=sec)).isoformat(timespec='milliseconds')

    def mmss(sec):
        return '%02d:%02d' % (int(sec) // 60, int(sec) % 60)

    pauses = []
    if dur >= 60:
        pauses = [(dur * 0.17, dur * 0.17 + 15, 'sum/pokret (los talas)', 'sum'),
                  (dur * 0.55, dur * 0.55 + 15, 'saturacija', 'sat')]

    def in_pause(sec):
        for a, b, lab, kind in pauses:
            if a <= sec < b:
                return lab, kind
        return None, None

    rr = 60.0 / hr
    lines = ['Timestamp,S1', '# START t=00:00 iso=%s label=snimanje' % iso(0)]
    last_q = -99.0
    for i in range(n):
        tsec = i / fs
        lab, kind = in_pause(tsec)
        base = 512 + 60 * math.sin(2 * math.pi * tsec / 7.0)
        if lab is None:
            ph = (tsec % rr) / rr
            v = base + (300 if ph < 0.035 else 60 if 0.25 <= ph < 0.32 else 0)
            v += rnd.gauss(0, 6)
        elif kind == 'sum':
            v = 512 + rnd.choice([1, -1]) * rnd.uniform(380, 470)
        else:
            v = 1023 if (i // 40) % 2 else 0
        v = max(0, min(1023, int(round(v))))
        for a, b, plab, pkind in pauses:
            if abs(tsec - a) < 0.5 / fs:
                st = 'SATUR.' if pkind == 'sat' else 'SUM'
                lines.append('# PAUSE t=%s iso=%s pp=%d jump=%s hf=%s sat=%d%% state=%s auto=1 label=%s'
                             % (mmss(a), iso(a), 950 if pkind == 'sum' else 1023,
                                '51.0' if pkind == 'sum' else '0.0',
                                '0.94' if pkind == 'sum' else '0.10',
                                5 if pkind == 'sum' else 76, st, plab))
            if abs(tsec - (b - 1.0 / fs)) < 0.5 / fs:
                lines.append('# RESUME t=%s iso=%s pause_s=%d pp=520 jump=0.0%% hf=0.30 sat=0%% state=OK '
                             'label=snimanje' % (mmss(b), iso(b), int(b - a)))
        if tsec - last_q >= 5:
            last_q = tsec
            st = 'SUM' if kind == 'sum' else 'SATUR.' if kind == 'sat' else 'OK'
            pp = 900 if kind == 'sum' else 1023 if kind == 'sat' else 330
            lines.append('# Q t=%s pp=%d jump=%s hf=%s sat=%d%% state=%s'
                         % (mmss(tsec), pp, '51.0' if kind == 'sum' else '0.0',
                            '0.94' if kind == 'sum' else '0.10',
                            5 if kind == 'sum' else 76 if kind == 'sat' else 0, st))
        lines.append('%s,%d' % (iso(tsec), v))
    lines.append('# STOP t=%s iso=%s samples=%d skipped_while_paused=0 paused_s=%d pauses=%d '
                 'gaps=0 gap_s=0 measured_fs=%.2f bad_lines=0 label=kraj'
                 % (mmss(dur), iso(dur), n, int(sum(b - a for a, b, _l, _k in pauses)),
                    len(pauses), fs))
    return '\n'.join(lines) + '\n', int(round(dur / rr))


def gen(kind, hr, dur, fs, seed):
    if kind == 'app':
        return gen_app(hr, dur, fs, seed)
    rnd = random.Random(seed)
    if kind == 'multi':
        # hr je spec "60:120,100:120,75:120" (bpm:sekundi) -> signal sa fazama,
        # za test scripts/ecg_phases.py (razlaganje dugog snimka po fazama)
        spec = []
        for part in str(hr).split(','):
            bpm, secs = part.split(':')
            spec.append((float(bpm), float(secs)))
        dur = sum(s for _, s in spec)
        n = int(fs * dur)
        t0 = datetime.datetime(2026, 10, 16, 10, 0, 0)
        kicks = []
        t = 0.3
        for bpm, secs in spec:
            end = t + secs
            rr = 60.0 / bpm
            while t < end:
                kicks.append(t)
                t += rr
        lines = ['Timestamp,S1']
        for i in range(n):
            t = i / fs
            v = 512.0
            for b in kicks:
                tk = t - b
                if -0.3 < tk < 0.5:
                    v += 300 * gauss(tk, 0.0, 0.012)
                    v -= 40 * gauss(tk, -0.015, 0.008)
                    v += 60 * gauss(tk, 0.35, 0.045)
                    v += 25 * gauss(tk, -0.16, 0.025)
            v += rnd.gauss(0, 6)
            v += 4 * math.sin(2 * math.pi * 0.25 * t)
            v = max(0, min(1023, int(round(v))))
            lines.append(f'{(t0 + datetime.timedelta(seconds=t)).isoformat(timespec="milliseconds")},{v}')
        return '\n'.join(lines) + '\n', len(kicks)
    n = int(fs * dur)
    t0 = datetime.datetime(2026, 10, 16, 10, 0, 0)
    lines = ['Timestamp,S1']
    # trenuci R vrhova
    kicks = []
    if kind == 'af':
        t = 0.3
        while t < dur - 0.5:
            kicks.append(t)
            t += rnd.uniform(0.35, 0.85)
    else:
        rr = 60.0 / hr
        t = 0.3
        while t < dur - 0.5:
            kicks.append(t)
            t += rr * (1 + (rnd.gauss(0, 0.01) if kind != 'brady' else 0))
    for i in range(n):
        t = i / fs
        v = 512.0
        for b in kicks:
            tk = t - b
            if -0.3 < tk < 0.5:
                v += 300 * gauss(tk, 0.0, 0.012)          # R
                v -= 40 * gauss(tk, -0.015, 0.008)        # Q
                v += 60 * gauss(tk, 0.35, 0.045) * (0 if kind == 'af' else 1)  # T (bez T kod af)
                v -= 25 * gauss(tk, 0.30, 0.030) * (0 if kind == 'af' else 1)  # S/ST
                if kind != 'af':
                    v += 25 * gauss(tk, -0.16, 0.025)     # P
        if kind == 'af':                                   # fibrilatorski talasi
            v += 18 * math.sin(2 * math.pi * 6.5 * t) + 10 * math.sin(2 * math.pi * 9.1 * t + 1.3)
        v += rnd.gauss(0, 6)
        v += 4 * math.sin(2 * math.pi * 0.25 * t)          # drift
        v = max(0, min(1023, int(round(v))))
        lines.append(f'{(t0 + datetime.timedelta(seconds=t)).isoformat(timespec="milliseconds")},{v}')
    return '\n'.join(lines) + '\n', len(kicks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['regular', 'af', 'brady', 'multi', 'app'])
    ap.add_argument('hr', nargs='?', default='112',
                    help="broj otkucaja ili, za kind=multi, spec faza: 60:120,100:120,75:120")
    ap.add_argument('dur', nargs='?', type=float, default=60.0)
    ap.add_argument('--fs', type=float, default=192.0)
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args()
    hr = a.hr if a.kind == 'multi' else float(a.hr)
    csv, nbeats = gen(a.kind, hr, a.dur, a.fs, a.seed)
    import sys
    sys.stdout.write(csv)
    print(f'# {a.kind}: {nbeats} udara u {a.dur:.0f}s', file=sys.stderr)


if __name__ == '__main__':
    main()
