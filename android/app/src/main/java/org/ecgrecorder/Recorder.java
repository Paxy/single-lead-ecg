package org.ecgrecorder;

import android.util.Log;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Živi prikaz + upis snimka + kontrola kontakta elektroda.
 *
 * Objekat postoji od trenutka kada je port otvoren (povezivanje) do prekida veze.
 * Pisanje u fajl je ODVOJENO (beginFile/endFile) — talas može da se gleda i bez snimanja.
 * Uzorci se upisuju UVEK dok je snimanje aktivno — i kada je signal loš. Loši delovi se
 * NE brišu nego se OBELЕŽE (`# PAUSE ... label=...` + metrički brojevi koji su je izazvali),
 * a analiza ih po tim markerima izuzima. Razlog (16.10.2026): brisanjem uzoraka gubi se
 * mogućnost ponovne analize, a rupa u vremenskoj osi se lako pročita kao „pauza u ritmu”
 * (servis je na snimku od 29.5 min prijavio 125 pauza i RR 36 s — sve artefakt).
 *
 * Format: `ISO_vreme,vrednost` po uzorku; kontrole `# START/# PAUSE/# RESUME/# GAP/# STOP/# MARK`
 * (svaka nosi `t=MM:SS`, `iso=` apsolutno vreme i brojeve stanja signala).
 */
public class Recorder {

    public static final int NOMINAL_FS = 192;
    public static final int CONTACT_OK = 0, CONTACT_WEAK = 1, CONTACT_FLAT = 2,
            CONTACT_SAT = 3, CONTACT_NOISE = 4;

    private static final String TAG = "EcgRecorder";
    /** Pragovi kvaliteta u ADC jedinicama (0-1023). */
    public static final int FLAT_PP = 20;      // ispod = ravna linija (elektroda otpala)
    public static final int WEAK_PP = 80;      // ispod = slab kontakt

    /** Podešavanje osetljivosti detekcije lošeg signala (korisnik bira dugmetom).
     *  VAŽNO: kriterijum je NAGLI SKOK + velika amplituda, a NE sadržaj visokih frekvencija —
     *  oštar R talas čistog ECG-a daje veliki HF ostatak, pa je prva verzija lažno prijavljivala šum. */
    public static final class Th {
        public final int jumpAbs;      // |razlika između uzastopnih uzoraka| koja se broji kao skok
        public final double jumpPct;   // % skokova u sekundi iznad koga je sumnjivo
        public final int ppSoft;       // amplituda koja uz skokove potvrđuje pokret
        public final int ppHard;       // amplituda koja sama po sebi znači artefakt
        final String name;
        Th(String n, int ja, double jp, int soft, int hard) {
            name = n; jumpAbs = ja; jumpPct = jp; ppSoft = soft; ppHard = hard;
        }
        public String label() { return name; }
    }
    private static final Th[] LEVELS = {
            new Th("Niska (najmanje lažnih)", 240, 2.2, 700, 980),
            new Th("Srednja", 180, 1.6, 600, 900),
            new Th("Visoka (stroga)", 140, 1.2, 500, 800),
    };
    private volatile int sensLevel = 1;
    private Th th() { return LEVELS[Math.max(0, Math.min(LEVELS.length - 1, sensLevel))]; }

    private static final long AUTO_PAUSE_MS = 2000;
    private static final long AUTO_RESUME_MS = 1000;
    private static final int LIVE_MS = 6000;
    private static final int Q_WIN_MS = 1000;   // prozor za procenu kvaliteta
    private static final int Q_LOG_MS = 5000;   // upis metrike kvaliteta u CSV (za podešavanje pragova)

    public interface Listener { void onError(String message); }

    private final Listener listener;
    private BufferedWriter out;
    private File file;

    private final StringBuilder lineBuf = new StringBuilder(64);
    private final LinkedBlockingQueue<String> marks = new LinkedBlockingQueue<>();

    private final ArrayDeque<double[]> live = new ArrayDeque<>();        // [tMs, v] za crtanje
    private final ArrayDeque<double[]> contactWin = new ArrayDeque<>();  // [tMs, v] za kontakt (2 s)
    private final ArrayDeque<Integer> qWin = new ArrayDeque<>();         // uzorci zadnje sekunde (kvalitet)
    private volatile double hfRatio;
    private volatile double jumpPctNow;
    private long lastQLogMs;
    private long ignoredMs;                      // koliko je signal odbačen (procena)

    private volatile boolean recording;
    private volatile boolean paused;
    private volatile boolean autoPaused;      // pauzu je uvela automatska zaštita (ne korisnik)
    private volatile String pauseReason = "";
    private volatile int contact = CONTACT_WEAK;
    private volatile int ppNow;
    private volatile int satPct;
    private long flatSince, okSince, pauseStart;
    private volatile long pausedMs;
    private long gapMs;
    private int gapCount;
    private long samples, writtenSamples, badLines, recStartWallMs;
    private int pauses;
    private boolean autoPause = true;

    private final ArrayDeque<Long> beats = new ArrayDeque<>();
    private double envelope, lastBeatT = -1;
    private volatile long lastSampleMs;
    private volatile int beatCount;
    private volatile long lastBeatMs;

    public Recorder(Listener listener) { this.listener = listener; }

    /** Koliko ms od poslednjeg primljenog uzorka (-1 = još nema podataka). */
    public long msSinceLastSample() {
        return lastSampleMs == 0 ? -1 : System.currentTimeMillis() - lastSampleMs;
    }
    public int beatCount() { return beatCount; }
    public long lastBeatMs() { return lastBeatMs; }
    /** Da li tok podataka stoji (npr. kabl ispao ili modul stao). */
    public boolean streamStalled() {
        long d = msSinceLastSample();
        return d < 0 || d > 2000;
    }

    // ------------------------------------------------------------------ fajl
    public synchronized boolean beginFile(File dir, String name) {
        try {
            if (!dir.exists() && !dir.mkdirs()) throw new IOException("ne mogu da napravim folder " + dir);
            file = new File(dir, name);
            out = new BufferedWriter(new OutputStreamWriter(
                    new FileOutputStream(file, false), StandardCharsets.UTF_8), 1 << 16);
            out.write("Timestamp,S1\n");
            out.write("# START t=00:00 iso=" + isoNow() + " label=snimanje\n");
            recStartWallMs = System.currentTimeMillis();
            samples = 0; writtenSamples = 0; badLines = 0; pausedMs = 0; pauses = 0;
            gapMs = 0; gapCount = 0;
            paused = false; pauseReason = ""; autoPaused = false;
            lastSampleMs = 0;
            contactWin.clear(); qWin.clear(); beats.clear(); live.clear(); marks.clear();
            recording = true;
            return true;
        } catch (IOException e) {
            fail("fajl: " + e.getMessage());
            return false;
        }
    }

    public static String isoNow() {
        return new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US).format(new Date());
    }

    public synchronized void endFile() {
        if (out != null) {
            try {
                out.write("# STOP t=" + mmss(elapsedMs() / 1000) + " iso=" + isoNow()
                        + " samples=" + writtenSamples
                        + " skipped_while_paused=" + Math.max(0, samples - writtenSamples)
                        + " paused_s=" + (pausedMs / 1000) + " pauses=" + pauses
                        + " gaps=" + gapCount + " gap_s=" + (gapMs / 1000)
                        + " measured_fs=" + String.format(Locale.US, "%.2f", measuredFs())
                        + " bad_lines=" + badLines + " label=kraj\n");
                out.flush();
                out.close();
            } catch (IOException e) {
                Log.e(TAG, "zatvaranje: " + e.getMessage());
            }
            out = null;
        }
        recording = false;
        paused = false;
    }

    public synchronized void setPaused(boolean p, String reason) {
        setPaused(p, reason, false);
    }

    /** @param bySystem true = pauzu je uvela automatska zaštita (loš signal), false = korisnik.
     *  ⚠️ Ranije se ovo izvodilo iz TEKSTA razloga i pucalo je: auto-pauza zbog šuma je
     *  prosleđivala „šum/pokret (loš talas)", što nije bilo ni „elektroda otpala" ni
     *  „saturacija", pa je zastavica ostajala false → upis se NIKAD nije sam nastavljao
     *  (na ekranu „signal OK" a i dalje „IGNORIŠEM"). Zato je sada eksplicitan parametar. */
    public synchronized void setPaused(boolean p, String reason, boolean bySystem) {
        if (!recording) return;
        if (p && !paused) {
            paused = true; pauseReason = reason; pauseStart = System.currentTimeMillis(); pauses++;
            autoPaused = bySystem;
            flushMarksNow();
            // Uzroci se i dalje upisuju (vidi zaglavlje klase) — marker nosi sve brojeve
            // koji su odluku izazvali, da pauza može da se proveri i posle snimanja.
            writeRaw("# PAUSE t=" + mmss(elapsedMs() / 1000) + " iso=" + isoNow()
                    + " pp=" + ppNow + " jump=" + jumpStr() + " hf=" + hfStr()
                    + " sat=" + satPct + "%" + " state=" + qualityTag()
                    + " auto=" + (bySystem ? 1 : 0)
                    + " label=" + safe(reason) + "\n");
        } else if (!p && paused) {
            long durS = pauseStart > 0 ? (System.currentTimeMillis() - pauseStart) / 1000 : 0;
            if (pauseStart > 0) pausedMs += System.currentTimeMillis() - pauseStart;
            paused = false; pauseReason = ""; autoPaused = false;
            writeRaw("# RESUME t=" + mmss(elapsedMs() / 1000) + " iso=" + isoNow()
                    + " pause_s=" + durS + " pp=" + ppNow + " jump=" + jumpStr()
                    + " hf=" + hfStr() + " sat=" + satPct + "%" + " state=" + qualityTag()
                    + " label=snimanje\n");
        }
    }

    /** Zaštitna mreža: ako je pauza automatska a signal je dobar duže od granice, nastavi upis —
     *  poziva se iz handleChunk nezavisno od ostalih uslova, da se ne može zaglaviti. */
    private void safetyResume(long nowMs) {
        if (!paused || !autoPaused || !autoPause || okSince <= 0) return;
        if (nowMs - okSince < AUTO_RESUME_MS) return;
        boolean bad = contact == CONTACT_FLAT || contact == CONTACT_SAT || contact == CONTACT_NOISE;
        if (!bad) setPaused(false, null, false);
    }

    public void setAutoPause(boolean on) { autoPause = on; }
    public boolean isAutoPause() { return autoPause; }
    /** Ručna pauza (auto se ne dira) — za dugme u UI-ju. */
    public synchronized void togglePause() {
        if (!recording) return;
        setPaused(!paused, paused ? null : "ručna pauza");
    }

    public boolean isRecording() { return recording; }
    public boolean isPaused() { return paused; }
    public String pauseReason() { return pauseReason; }
    public File file() { return file; }
    public long writtenSamples() { return writtenSamples; }
    public long skippedSamples() { return Math.max(0, samples - writtenSamples); }
    public long badLines() { return badLines; }
    public int pauses() { return pauses; }
    public long gapSeconds() { return gapMs / 1000; }
    public int gaps() { return gapCount; }
    public long pausedSeconds() { return pausedMs / 1000 + (paused && pauseStart > 0
            ? (System.currentTimeMillis() - pauseStart) / 1000 : 0); }
    public int contact() { return contact; }
    public int pp() { return ppNow; }
    public int satPct() { return satPct; }
    public double hfRatio() { return hfRatio; }
    public double jumpPct() { return jumpPctNow; }
    public long ignoredSeconds() { return ignoredMs / 1000 + (paused && pauseStart > 0
            ? (System.currentTimeMillis() - pauseStart) / 1000 : 0); }

    public String contactText() {
        switch (contact) {
            case CONTACT_OK: return "kontakt: DOBAR (pp " + ppNow + ", šum " + pct(hfRatio) + ")";
            case CONTACT_WEAK: return "kontakt: SLAB (pp " + ppNow + ") — pritisni elektrodu, očisti kožu";
            case CONTACT_FLAT: return "kontakt: OTPALA ELEKTRODA — ravna linija (pp " + ppNow + ")";
            case CONTACT_NOISE: return "kontakt: ŠUM/POKRET (pp " + ppNow + ", šum " + pct(hfRatio)
                    + ") — upisujem sirovo i označavam kao loše (analiza izuzima)";
            default: return "kontakt: SATURACIJA " + satPct + "% — smanji pojačanje";
        }
    }

    private static String pct(double x) { return String.format(Locale.US, "%.0f%%", x * 100); }

    private String hfStr() { return String.format(Locale.US, "%.2f", hfRatio); }

    private String jumpStr() { return String.format(Locale.US, "%.1f%%", jumpPctNow); }

    /** Kratka oznaka stanja signala za indikator u UI-ju. */
    public String qualityTag() {
        switch (contact) {
            case CONTACT_OK: return "OK";
            case CONTACT_WEAK: return "SLAB";
            case CONTACT_FLAT: return "RAVNA";
            case CONTACT_NOISE: return "ŠUM";
            default: return "SATUR.";
        }
    }

    /** Zašto se trenutno ignoriše signal (ili prazno ako se upisuje). */
    public String ignoreLabel() {
        if (!paused) return "";
        String base;
        if (pauseReason == null || pauseReason.isEmpty()) base = "pauza";
        else if (pauseReason.startsWith("elektroda")) base = "ravna linija (elektroda otpala)";
        else if (pauseReason.startsWith("šum")) base = "šum/pokret";
        else if (pauseReason.startsWith("saturacija")) base = "saturacija";
        else base = pauseReason;
        return base + (autoPaused ? "  [auto]" : "  [ručno]");
    }

    public double elapsedMs() {
        return recStartWallMs == 0 ? 0 : System.currentTimeMillis() - recStartWallMs;
    }

    public double measuredFs() {
        double el = elapsedMs() / 1000.0;
        return el > 1 ? samples / el : 0;
    }

    public String liveHr() {
        long t = System.currentTimeMillis();
        while (!beats.isEmpty() && t - beats.peekFirst() > 15000) beats.pollFirst();
        if (beats.size() < 3) return "-";
        List<Long> rr = new ArrayList<>();
        Long prev = null;
        for (Long b : beats) { if (prev != null) rr.add(b - prev); prev = b; }
        Collections.sort(rr);
        long med = rr.get(rr.size() / 2);
        return med > 0 ? String.valueOf(Math.round(60000.0 / med)) : "-";
    }

    public void mark(String label) {
        marks.offer(label == null || label.trim().isEmpty() ? "mark" : label.trim());
    }

    public synchronized List<double[]> trace(long ms) {
        long t = System.currentTimeMillis();
        List<double[]> o = new ArrayList<>();
        for (double[] p : live) if (t - p[0] <= ms) o.add(p);
        return o;
    }

    // ---------------------------------------------------------------- podaci
    public void onData(byte[] data) {
        long now = System.currentTimeMillis();
        List<Integer> vals = new ArrayList<>(data.length / 5 + 4);
        for (byte b : data) {
            char c = (char) (b & 0xFF);
            if (c == '\n' || c == '\r') {
                if (lineBuf.length() > 0) {
                    String s = lineBuf.toString().trim();
                    lineBuf.setLength(0);
                    if (!s.isEmpty()) {
                        try {
                            int v = Integer.parseInt(s);
                            if (v >= 0 && v <= 4095) vals.add(v); else badLines++;
                        } catch (NumberFormatException e) { badLines++; }
                    }
                }
            } else if (lineBuf.length() < 24) {
                lineBuf.append(c);
            }
        }
        if (!vals.isEmpty()) handleChunk(vals, now);
    }

    private synchronized void handleChunk(List<Integer> vals, long nowMs) {
        double step = 1000.0 / NOMINAL_FS;
        // Prekid toka podataka: uzorci nisu stizali duže od 3 s (telefon/USB je zastao).
        // Ovo NIJE srčana pauza — upisujemo je u fajl da analiza ne bi prazninu protumačila
        // kao prekid ritma (realan slučaj: 71 s izgubljenih uzoraka → servis prijavio „RR 72 s").
        if (recording && lastSampleMs > 0) {
            long d = nowMs - lastSampleMs;
            if (d > 3000) {
                gapCount++;
                gapMs += d;
                writeRaw(String.format(Locale.US,
                        "# GAP t=%s iso=%s s=%.1f label=prekid toka podataka (nije srčana pauza)\n",
                        mmss(elapsedMs() / 1000), isoNow(), d / 1000.0));
                contactWin.clear();
                qWin.clear();
                if (logger != null) logger.accept(String.format(Locale.US, "PREKID TOKA PODATAKA %.0f s (telefon/USB)", d / 1000.0));
            }
        }
        for (int i = 0; i < vals.size(); i++) {
            double ts = nowMs - (vals.size() - 1 - i) * step;
            pushLive(ts, vals.get(i));
        }
        samples += vals.size();
        lastSampleMs = nowMs;
        updateContact(nowMs);

        if (recording) {
            boolean bad = contact == CONTACT_FLAT || contact == CONTACT_SAT || contact == CONTACT_NOISE;
            if (autoPause && !paused && bad && flatSince > 0 && nowMs - flatSince > AUTO_PAUSE_MS) {
                String why = contact == CONTACT_FLAT ? "elektroda otpala (ravna linija)"
                        : contact == CONTACT_SAT ? "saturacija" : "šum/pokret (loš talas)";
                setPaused(true, why, true);
            } else if (paused && autoPause && !bad && okSince > 0
                    && nowMs - okSince > AUTO_RESUME_MS && autoPaused) {
                setPaused(false, null, false);
            }
            safetyResume(nowMs);        // nezavisna zaštita od zaglavljivanja u pauzi
            if (paused && autoPaused) ignoredMs += (long) vals.size() * 1000 / NOMINAL_FS;
            if (nowMs - lastQLogMs > Q_LOG_MS) {
                lastQLogMs = nowMs;
                writeRaw(String.format(Locale.US, "# Q t=%s pp=%d jump=%.1f%% hf=%.2f sat=%d%% state=%s%s\n",
                        mmss(elapsedMs() / 1000), ppNow, jumpPctNow, hfRatio, satPct, qualityTag(),
                        paused ? " IGNORISEM=" + ignoreLabel() : ""));
            }
        }

        if (recording) {
            // Upis ide i u pauzi (sirovi uzorci) — analiza ih izbacuje po markerima,
            // a mi ne gubimo podatke koje više nikad ne možemo da rekonstruišemo.
            flushMarksNow();
            writeSamples(vals, nowMs, step);
        }
    }

    private void writeSamples(List<Integer> vals, long nowMs, double step) {
        SimpleDateFormat fmt = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US);
        StringBuilder sb = new StringBuilder(vals.size() * 36);
        for (int i = 0; i < vals.size(); i++) {
            long ts = Math.round(nowMs - (vals.size() - 1 - i) * step);
            sb.append(fmt.format(new Date(ts))).append(',').append(vals.get(i)).append('\n');
        }
        writeRaw(sb.toString());
        writtenSamples += vals.size();
    }

    private synchronized void writeRaw(String s) {
        if (out == null) return;
        try {
            out.write(s);
            if (writtenSamples % 5120 < 8) out.flush();
        } catch (IOException e) {
            fail("upis: " + e.getMessage());
        }
    }

    private void flushMarksNow() {
        String m;
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US);
        while ((m = marks.poll()) != null) {
            writeRaw("# MARK t=" + mmss(elapsedMs() / 1000) + " at=" + f.format(new Date())
                    + " label=" + safe(m) + "\n");
        }
    }

    // --------------------------------------------------------------- kontakt
    private void updateContact(long nowMs) {
        while (!contactWin.isEmpty() && nowMs - contactWin.peekFirst()[0] > 2000) contactWin.pollFirst();
        // sekundni prozor za kvalitet
        while (qWin.size() > NOMINAL_FS) qWin.pollFirst();
        if (contactWin.size() < 60) { contact = CONTACT_WEAK; ppNow = 0; return; }
        int mn = Integer.MAX_VALUE, mx = Integer.MIN_VALUE, sat = 0;
        for (double[] p : contactWin) {
            int v = (int) p[1];
            if (v < mn) mn = v;
            if (v > mx) mx = v;
            if (v <= 1 || v >= 1022) sat++;
        }
        ppNow = mx - mn;
        satPct = (int) Math.round(100.0 * sat / contactWin.size());
        hfRatio = computeHfRatio();     // samo dijagnostika (prikaz u UI i u # Q liniji)
        jumpPctNow = computeJumpPct();
        Th t = th();

        // pokret = mnogo NAGLIH skokova + (velika amplituda ILI saturacija ILI nemoguć puls)
        boolean motion = jumpPctNow > t.jumpPct && (ppNow > t.ppSoft || satPct > 5 || absurdHr());
        if (satPct > 50) contact = CONTACT_SAT;
        else if (ppNow < FLAT_PP) contact = CONTACT_FLAT;
        else if (motion || ppNow > t.ppHard) contact = CONTACT_NOISE;
        else if (ppNow < WEAK_PP) contact = CONTACT_WEAK;
        else contact = CONTACT_OK;

        // „loše" stanje za auto-pauzu: ravna linija / saturacija / šum-pokret.
        // SLAB kontakt NIJE loše — snimak se upisuje (samo upozorenje), pa i povratak
        // iz pauze mora da se osloni na ovo, a ne na „kontakt je DOBAR" (to je bio bug:
        // uz slab kontakt uslov za nastavak nikad nije bio ispunjen → zauvek „ignorišem").
        boolean badNow = contact == CONTACT_FLAT || contact == CONTACT_SAT || contact == CONTACT_NOISE;
        if (badNow) {
            if (flatSince == 0) flatSince = nowMs;
            okSince = 0;
        } else {
            if (okSince == 0) okSince = nowMs;
            flatSince = 0;
        }
    }

    /** % uzoraka u zadnjoj sekundi sa naglim skokom (pokret), sa pragom koji se skalira sa
     *  amplitudom signala — inače sam uspon R talasa (koji je ~0.5×pp po uzorku) ispadne „skok". */
    private double computeJumpPct() {
        if (qWin.size() < 32) return 0;
        int[] x = new int[qWin.size()];
        int i = 0;
        for (Integer v : qWin) x[i++] = v;
        int thr = Math.max(th().jumpAbs, (int) (0.75 * ppNow));
        int jumps = 0;
        for (int j = 1; j < x.length; j++) if (Math.abs(x[j] - x[j - 1]) > thr) jumps++;
        return 100.0 * jumps / x.length;
    }

    /** Da li detektor udara daje nemoguć (prebrz) ritam — znak artefakta. */
    private boolean absurdHr() {
        if (beats.size() < 12) return false;
        List<Long> rr = new ArrayList<>();
        Long prev = null;
        for (Long b : beats) { if (prev != null) rr.add(b - prev); prev = b; }
        if (rr.size() < 10) return false;
        Collections.sort(rr);
        long med = rr.get(rr.size() / 2);
        return med > 0 && med < 280;    // > 214/min
    }

    public void cycleSensitivity() {
        sensLevel = (sensLevel + 1) % LEVELS.length;
        logEventLocal("osetljivost: " + th().label());
    }

    public String sensitivityLabel() { return th().label(); }

    private static java.util.function.Consumer<String> logger;
    public static void setLogger(java.util.function.Consumer<String> l) { logger = l; }
    private static void logEventLocal(String s) { if (logger != null) logger.accept(s); }

    /** Udeo “visokofrekventnog” ostatka u signalu: RMS(x - pokretni prosek) / RMS(x - DC).
     *  Mirovanje sa dobrim kontaktom daje ~0.1-0.3; pokret/šum diže iznad ~0.45. */
    private double computeHfRatio() {
        if (qWin.size() < 64) return 0;
        int[] x = new int[qWin.size()];
        int i = 0;
        for (Integer v : qWin) x[i++] = v;
        int n = x.length;
        double sum = 0;
        for (int v : x) sum += v;
        double dc = sum / n;
        double varSum = 0, resSum = 0;
        int k = 4;                                  // prosek ±4 uzorka (~21 ms → ~20 Hz)
        for (int j = 0; j < n; j++) {
            double d = x[j] - dc;
            varSum += d * d;
            int a = Math.max(0, j - k), b = Math.min(n - 1, j + k), c = 0;
            double s = 0;
            for (int m = a; m <= b; m++) { s += x[m]; c++; }
            double lp = s / c;
            double r = x[j] - lp;
            resSum += r * r;
        }
        if (varSum <= 1e-6) return 0;
        return Math.sqrt(resSum / n) / Math.sqrt(varSum / n);
    }

    private void pushLive(double tMs, int v) {
        live.addLast(new double[]{tMs, v});
        contactWin.addLast(new double[]{tMs, v});
        qWin.addLast(v);
        while (qWin.size() > NOMINAL_FS) qWin.pollFirst();
        while (!live.isEmpty() && tMs - live.peekFirst()[0] > LIVE_MS) live.pollFirst();
        if (live.size() < 30) return;
        double sum = 0;
        for (double[] p : live) sum += p[1];
        double dc = sum / live.size();
        double x = v - dc;
        envelope = Math.max(envelope * 0.995, x);
        if (x > 0.5 * envelope && (lastBeatT < 0 || tMs - lastBeatT > 250)) {
            lastBeatT = tMs;
            lastBeatMs = (long) tMs;
            beatCount++;
            beats.addLast((long) tMs);
            while (beats.size() > 40) beats.pollFirst();
        }
    }

    private static String safe(String s) { return s == null ? "" : s.replace('\n', ' '); }

    private static String mmss(double sec) {
        long s = (long) sec;
        return String.format(Locale.US, "%02d:%02d", s / 60, s % 60);
    }

    private void fail(String msg) {
        Log.e(TAG, msg);
        if (listener != null) listener.onError(msg);
    }
}
