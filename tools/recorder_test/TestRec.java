import org.ecgrecorder.Recorder;

import java.io.File;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Test state-mašine auto-pauze u Recorder.java (bez telefona).
 *
 * Scenario (svaki korak traje realno vreme, jer Recorder koristi System.currentTimeMillis()):
 *   A) čist signal 8 s        → nema pauze, upis ide, kontakt OK
 *   B) šum/pokret 5 s         → auto-pauza sa razlogom „šum/pokret" i oznakom [auto]
 *   C) ponovo čist signal 6 s → SAM se vraća (RESUME)   ← ključna klasa greške
 *   D) slab kontakt 8 s       → NEMA pauze (slab kontakt se upisuje)
 *   E) šum 4 s pa slab 6 s    → pauza zbog šuma, pa nastavak iako kontakt nije „DOBAR"
 *   F) CSV: svaki # PAUSE ima svoj # RESUME (nema zaglavljivanja)
 */
public class TestRec {

    static final int FS = Recorder.NOMINAL_FS;
    static final Random RND = new Random(7);
    static final List<String> fails = new ArrayList<>();
    static double phase = 0;

    /** clean = mali spor talas + QRS svakih 0.86 s (70/min) */
    static int clean(double t) {
        double v = 512 + 180 * Math.sin(2 * Math.PI * 0.25 * t);
        double beat = t % 0.86;
        if (beat < 0.02) v += 260;
        else if (beat < 0.05) v += 90;
        return (int) Math.max(0, Math.min(1023, v));
    }

    static int noisy(double t) {
        return (int) Math.max(0, Math.min(1023, 512 + (RND.nextBoolean() ? 1 : -1) * (400 + RND.nextInt(120))));
    }

    static int weak(double t) {
        return (int) (512 + 25 * Math.sin(2 * Math.PI * 1.2 * t));
    }

    /** pušta `sec` sekundi signala u chunkovima od 0.05 s i realno spava toliko */
    static void feed(Recorder r, double sec, int kind) throws Exception {
        int chunks = (int) (sec * 20);
        for (int c = 0; c < chunks; c++) {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < FS / 20; i++) {
                phase += 1.0 / FS;
                int v = kind == 0 ? clean(phase) : kind == 1 ? noisy(phase) : weak(phase);
                sb.append(v).append('\n');
            }
            r.onData(sb.toString().getBytes(StandardCharsets.US_ASCII));
            Thread.sleep(50);
        }
    }

    static int nChecks = 0;

    static void check(boolean ok, String msg) {
        nChecks++;
        System.out.println((ok ? "  PASS  " : "  FAIL  ") + msg);
        if (!ok) fails.add(msg);
    }

    /** Broj uzoraka upisanih IZMEDJU # PAUSE i # RESUME — od 2.1 mora biti > 0. */
    static int countSamplesInsidePauses(String csv) {
        int inside = 0; boolean paused = false;
        for (String line : csv.split("\n")) {
            if (line.startsWith("# PAUSE")) paused = true;
            else if (line.startsWith("# RESUME")) paused = false;
            else if (paused && !line.startsWith("#") && line.contains(",")) inside++;
        }
        return inside;
    }

    static String read(File f) throws Exception {
        try (RandomAccessFile raf = new RandomAccessFile(f, "r")) {
            byte[] b = new byte[(int) raf.length()];
            raf.readFully(b);
            return new String(b, StandardCharsets.UTF_8);
        }
    }

    public static void main(String[] args) throws Exception {
        Recorder r = new Recorder(s -> { });
        File dir = new File(System.getProperty("java.io.tmpdir"), "rectest");
        dir.mkdirs();
        File[] old = dir.listFiles();
        if (old != null) for (File f : old) f.delete();

        if (!r.beginFile(dir, "test.csv")) { System.out.println("Ne mogu da otvorim fajl"); return; }
        Thread.sleep(500);

        System.out.println("A) čist signal 8 s");
        feed(r, 8, 0);
        System.out.println("   paused=" + r.isPaused() + " contact=" + r.contact() + " pp=" + r.pp()
                + " skokovi=" + String.format("%.1f%%", r.jumpPct()) + " upisano=" + r.writtenSamples());
        check(!r.isPaused(), "posle 8 s čistog signala NE sme biti pauze (lažni šum na R talasu)");
        check(r.contact() == Recorder.CONTACT_OK, "kontakt mora biti OK pri pp ~" + r.pp());

        System.out.println("B) šum/pokret 5 s");
        boolean paused = false;
        for (int i = 0; i < 5 && !paused; i++) { feed(r, 1, 1); paused = r.isPaused(); }
        System.out.println("   paused=" + r.isPaused() + " razlog=" + r.ignoreLabel()
                + " contact=" + r.contact() + " pp=" + r.pp());
        check(paused, "šum mora uvesti auto-pauzu u roku od nekoliko sekundi");
        check(r.ignoreLabel().contains("[auto]"), "pauza mora biti označena kao AUTOMATSKA");

        System.out.println("C) ponovo čist signal 6 s → mora SAM da nastavi");
        feed(r, 6, 0);
        System.out.println("   paused=" + r.isPaused() + " contact=" + r.contact() + " upisano=" + r.writtenSamples());
        check(!r.isPaused(), "posle 6 s čistog signala upis mora biti nastavljen (ključni bug)");

        System.out.println("D) slab kontakt 8 s → ne sme da pauzira");
        feed(r, 8, 2);
        System.out.println("   paused=" + r.isPaused() + " contact=" + r.contact() + " pp=" + r.pp());
        check(!r.isPaused(), "slab kontakt se upisuje — ne sme u pauzu");

        System.out.println("E) šum 4 s pa slab kontakt 6 s → pauza pa nastavak");
        for (int i = 0; i < 6 && !r.isPaused(); i++) feed(r, 1, 1);
        boolean wasPaused = r.isPaused();
        feed(r, 6, 2);
        System.out.println("   pauzirao=" + wasPaused + " → sada paused=" + r.isPaused()
                + " contact=" + r.contact() + " upisano=" + r.writtenSamples());
        check(wasPaused, "šum je morao da pauzira");
        check(!r.isPaused(), "sa slabim (ali ne lošim) kontaktom upis mora da se nastavi");

        r.endFile();

        String csv = read(new File(dir, "test.csv"));
        int nPause = csv.split("# PAUSE", -1).length - 1;
        int nResume = csv.split("# RESUME", -1).length - 1;
        String stop = csv.substring(csv.lastIndexOf("# STOP"));
        System.out.println("\nCSV: # PAUSE=" + nPause + "  # RESUME=" + nResume);
        System.out.println(stop.trim());
        check(nPause >= 2 && nResume >= 2, "svaka pauza mora imati i RESUME");
        check(nPause == nResume, "broj PAUSE i RESUME mora biti jednak");

        int inPause = countSamplesInsidePauses(csv);
        System.out.println("uzoraka upisanih TOKOM pauze: " + inPause);
        check(inPause > 100, "uzorci se upisuju i tokom pauze (sirovo, oznaceno markerima)");
        check(csv.contains(" state=") && csv.contains(" iso=") && csv.contains(" pp="),
                "# PAUSE/RESUME nose iso= vreme i brojeve stanja (pp/jump/sat/state)");
        check(csv.contains("# GAP") ? csv.contains("label=prekid toka podataka") : true,
                "# GAP nosi label kao poslednje polje");

        System.out.println();
        if (fails.isEmpty()) System.out.println("=== SVI TESTOVI PROŠLI (" + nChecks + "/" + nChecks + ") ===");
        else { System.out.println("=== PADOVI ==="); for (String f : fails) System.out.println(" - " + f); System.exit(1); }
    }
}
