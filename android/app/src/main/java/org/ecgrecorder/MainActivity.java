package org.ecgrecorder;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.util.Locale;

/**
 * UI v1.3: natpis stanja, živi talas, dugmad, log na ekranu (+ kopiranje).
 *
 * Ključna razlika u odnosu na 1.1/1.2: akcije se izvršavaju DIREKTNO preko žive
 * instance servisa (ista aplikacija i proces), a intent se koristi samo ako servis
 * još ne postoji. Tako klik ne može da „iščezne“ u poruci koja se ne obradi.
 */
public class MainActivity extends Activity {

    public static final String APP_VERSION = "2.1";
    // Kada je uključeno, posle „Stop i sačuvaj“ aplikacija se zatvara (servis se zaustavlja,
    // USB port se oslobađa). Ne zatvara se ako snimak nije sačuvan — tada ostaje otvorena.
    private boolean closeAfterStop;
    private static final String PREFS = "ecgrec";
    private static final String K_CLOSE_AFTER_STOP = "close_after_stop";
    private boolean keepAwake = true;   // ekran ne sme da se ugasi dok gledaš signal

    private TextView header, banner, statusView, statsView, uploadView, hintView, logView;
    private android.widget.CheckBox chkClose;
    private LiveView wave;
    private Button btnPause, btnAuto;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private File lastFile;
    private boolean blink = false;
    private int clicks = 0;

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            try { render(); } catch (Throwable t) { logView.setText("UI greška: " + t); }
            ui.postDelayed(this, 300);
        }
    };

    private void render() {
        Recorder r = RecService.recorder;
        boolean connected = r != null;
        boolean rec = connected && r.isRecording();
        blink = !blink;

        header.setText(String.format(Locale.US, "v%s · servis: %s · veza: %s · klikova: %d · osetljivost: %s · ekran: %s",
                APP_VERSION, RecService.instance != null ? "aktivan" : "ne",
                connected ? "da" : "ne", clicks,
                r != null ? r.sensitivityLabel() : "—",
                keepAwake ? "UKLJUČEN" : "može da se ugasi"));

        if (rec) {
            long stale = r.msSinceLastSample();
            String tok = stale < 0 ? "nema podataka" : (stale < 1500 ? "tok OK"
                    : "NEMA PODATAKA " + (stale / 1000) + " s!");
            if (r.isPaused()) {
                banner.setBackgroundColor(0xFFB26A00);
                banner.setText(String.format(Locale.US, "%s IGNORISEM: %s   (signal %s)\npp %d · skokovi %.1f%% · prekid %d s · preskočeno ukupno %d s",
                        blink ? "⏸" : " ", r.ignoreLabel(), r.qualityTag(), r.pp(), r.jumpPct(),
                        r.pausedSeconds(), r.ignoredSeconds()));
            } else {
                banner.setBackgroundColor(0xFFB00020);
                banner.setText(String.format(Locale.US, "%s SNIMAM  %02d:%02d:%02d   signal %s\nuzoraka %d · puls %s/min · %s",
                        blink ? "●" : " ", (long) (r.elapsedMs() / 3600000),
                        (long) (r.elapsedMs() / 60000) % 60, (long) (r.elapsedMs() / 1000) % 60,
                        r.qualityTag(), r.writtenSamples(), r.liveHr(), tok));
            }
        } else if (connected) {
            banner.setBackgroundColor(0xFF1B5E20);
            banner.setText(String.format(Locale.US, "● POVEZANO — snimanje NIJE pokrenuto\nsignal %s · pp %d · puls %s/min",
                    r.qualityTag(), r.pp(), r.liveHr()));
        } else {
            banner.setBackgroundColor(0xFF37474F);
            banner.setText("○ nije povezano\npriključi modul preko OTG pa pritisni „Poveži uređaj“");
        }

        statusView.setText("stanje: " + RecService.status
                + (RecService.lastError.isEmpty() ? "" : "\n! " + RecService.lastError)
                + "\nservis: " + (RecService.instance != null ? "aktivan" : "nije aktivan")
                + "   veza: " + (connected ? "da" : "ne"));

        if (connected) {
            wave.update(r.trace(5000), r.pp(), r.contact(), r.contactText(),
                    r.lastBeatMs() > 0 ? System.currentTimeMillis() - r.lastBeatMs() : -1);
            hintView.setText(hint(r));
            StringBuilder sb = new StringBuilder();
            if (rec) {
                lastFile = r.file();
                sb.append("puls (procena): ").append(r.liveHr()).append(" /min\n");
                sb.append("upisano: ").append(r.writtenSamples()).append(" uzoraka (svi, i loši)")
                        .append(", označeno kao loše: ").append(r.ignoredSeconds()).append(" s u ")
                        .append(r.pauses()).append(" delova\n");
                sb.append(String.format(Locale.US, "izmereno: %.1f Hz · pp %d · skokovi %.1f%% · pauza %d s / %d puta · prekid toka %d s / %d puta%n",
                        r.measuredFs(), r.pp(), r.jumpPct(), r.pausedSeconds(), r.pauses(),
                        r.gapSeconds(), r.gaps()));
                sb.append("fajl: ").append(r.file() == null ? "-"
                        : r.file().getName() + "  (" + r.file().length() / 1024 + " KB)");
                sb.append("\n").append(r.file() == null ? "" : r.file().getAbsolutePath());
            } else {
                sb.append("pp amplituda: ").append(r.pp())
                        .append(" · skokovi ").append(String.format(Locale.US, "%.1f%%", r.jumpPct()))
                        .append(" · neispravnih linija: ").append(r.badLines());
                if (lastFile != null) sb.append("\nposlednji snimak: ").append(lastFile.getName())
                        .append(" (").append(lastFile.length() / 1024).append(" KB)");
            }
            statsView.setText(sb.toString());
        } else {
            wave.update(null, 0, Recorder.CONTACT_WEAK, "", -1);
            hintView.setText("1) Priključi modul preko OTG   2) „Poveži uređaj“   3) dozvoli USB pristup");
            statsView.setText(lastFile != null
                    ? "poslednji snimak: " + lastFile.getName() + " (" + lastFile.length() / 1024 + " KB)\n"
                      + lastFile.getAbsolutePath()
                    : "nema snimka još");
        }

        btnPause.setText(rec && r.isPaused() ? "Nastavi" : "Pauza");
        btnAuto.setText(r != null && r.isAutoPause() ? "Auto-pauza: UKLJ." : "Auto-pauza: ISKLJ.");

        StringBuilder lg = new StringBuilder();
        synchronized (RecService.log) {
            int n = RecService.log.size(), i = 0;
            for (String s : RecService.log) {
                if (i++ >= Math.max(0, n - 8)) lg.append(s).append('\n');
            }
        }
        logView.setText(lg.toString().trim());
    }

    private String hint(Recorder r) {
        switch (r.contact()) {
            case Recorder.CONTACT_OK:
                return "Signál dobar — vidiš li pravilan talas sa oštrim skokom (R) i manjim bregom (T)?\n"
                     + "Ako da, elektrode su dobro postavljene → „▶ Start snimanja“.";
            case Recorder.CONTACT_WEAK:
                return "Slab kontakt (pp " + r.pp() + "): očisti kožu, nalepi elektrodu čvršće, pomeri bliže srcu.";
            case Recorder.CONTACT_FLAT:
                return "RAVNA LINIJA — elektroda je otpala ili modul nema napajanja. Proveri sve tri elektrode i kabl.";
            case Recorder.CONTACT_NOISE:
                return "LOŠ TALAS (šum/pokret) — uzorci se upisuju, ali se taj deo u snimku OZNAČAVA\n"
                     + "(# PAUSE sa brojevima) i analiza ga izuzima. Umiri ruku i skloni telefon/kabl od elektroda.";
            default:
                return "Saturacija: signal udara u granice opsega — smanji pojačanje modula.";
        }
    }

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        int p = (int) (10 * getResources().getDisplayMetrics().density);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(p, p, p, p);
        scroll.addView(root);

        banner = new TextView(this);
        banner.setTextSize(16);
        banner.setTextColor(Color.WHITE);
        banner.setPadding(p, p, p, p);
        banner.setBackgroundColor(0xFF37474F);
        banner.setTypeface(android.graphics.Typeface.MONOSPACE);
        banner.setMinLines(2);          // fiksirane dve linije — tekst ispod se ne pomera
        header = new TextView(this);
        header.setTextSize(12);
        header.setTypeface(android.graphics.Typeface.MONOSPACE);
        root.addView(header);
        root.addView(banner);

        wave = new LiveView(this, null);
        wave.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                (int) (140 * getResources().getDisplayMetrics().density)));
        root.addView(wave);

        hintView = tv(root, 13, p);
        statusView = tv(root, 14, p);
        statsView = tv(root, 14, p);

        root.addView(row(btn("Poveži uređaj", v -> act(RecService.ACTION_CONNECT, null, "Povezujem…")),
                btn("▶ Start snimanja", v -> act(RecService.ACTION_REC_START, null, "Start snimanja…"))));
        btnPause = btn("Pauza", v -> act(RecService.ACTION_REC_PAUSE_TOGGLE, null, "Pauza/nastavak…"));
        Button btnStop = btn("■ Stop i sačuvaj", v -> {
            act(RecService.ACTION_REC_STOP, null, "Zaustavljam…");
            if (closeAfterStop) waitThenClose(0);
        });
        root.addView(row(btnPause, btnStop));

        btnAuto = btn("Auto-pauza: UKLJ.", v -> act(RecService.ACTION_AUTO_TOGGLE, null, "Menjam auto-pauzu…"));
        chkClose = new android.widget.CheckBox(this);
        chkClose.setText("Zatvori aplikaciju kad se snimanje završi (posle „Stop“)");
        chkClose.setTextSize(13);
        chkClose.setChecked(closeAfterStop);
        chkClose.setOnClickListener(v -> {
            closeAfterStop = chkClose.isChecked();
            getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                    .putBoolean(K_CLOSE_AFTER_STOP, closeAfterStop).apply();
            RecService.logEvent("gašenje posle snimanja: " + (closeAfterStop ? "UKLJUČENO" : "ISKLJUČENO"));
            Toast.makeText(this, closeAfterStop
                            ? "Posle Stop-a aplikacija će se sama zatvoriti (ako je snimak sačuvan)"
                            : "Aplikacija ostaje otvorena posle snimanja",
                    Toast.LENGTH_LONG).show();
        });
        root.addView(chkClose);

        root.addView(row(btnAuto, btn("Osetljivost", v -> act(RecService.ACTION_SENS_TOGGLE, null, "Menjam osetljivost…")),
                btn("Ekran", v -> {
                    keepAwake = !keepAwake;
                    applyScreenFlag();
                    RecService.logEvent("ekran: " + (keepAwake ? "držim uključen" : "može da se ugasi"));
                    Toast.makeText(this, keepAwake ? "Ekran: držim uključen" : "Ekran: može da se ugasi",
                            Toast.LENGTH_SHORT).show();
                }),
                btn("Prekini vezu", v -> act(RecService.ACTION_DISCONNECT, null, "Prekidam vezu…"))));
        root.addView(row(btn("Reset stanja", v -> act(RecService.ACTION_RESET, null, "Resetujem stanje…")),
                btn("Ugasi aplikaciju", v -> {
                    Recorder r = RecService.recorder;
                    if (r != null && r.isRecording()) {
                        Toast.makeText(this, "Prvo „Stop i sačuvaj“ — snimanje je u toku",
                                Toast.LENGTH_LONG).show();
                        RecService.logEvent("gašenje odbijeno: snimanje je u toku");
                        return;
                    }
                    Toast.makeText(this, "Zatvaram aplikaciju…", Toast.LENGTH_SHORT).show();
                    RecService.logEvent("ručno gašenje aplikacije");
                    closeApp();
                })));

        TextView lbl = tv(root, 13, p);
        lbl.setText("Marker faze (upisuje se u snimak):");
        String[] markers = {"mirovanje", "ustao", "legao", "posle kupke", "stres", "kafa", "posle treninga", "kraj"};
        HorizontalScrollView hs = new HorizontalScrollView(this);
        LinearLayout mrow = new LinearLayout(this);
        mrow.setOrientation(LinearLayout.HORIZONTAL);
        for (String m : markers) mrow.addView(btn(m, v -> act(RecService.ACTION_MARK, m, "Marker: " + m)));
        hs.addView(mrow);
        root.addView(hs);

        root.addView(row(btn("Pošalji na servis", v -> upload()),
                btn("Otvori nalaz", v -> startActivity(new Intent(Intent.ACTION_VIEW,
                        Uri.parse("https://example.com/ecg/index.php"))))));
        uploadView = tv(root, 13, p);

        root.addView(row(btn("Kopiraj log", v -> copyLog()),
                btn("Test upisa", v -> testWrite()),
                btn("Baterija/pozadina", v -> batteryDialog()),
                btn("Očisti log", v -> { synchronized (RecService.log) { RecService.log.clear(); } })));

        TextView lblLog = tv(root, 13, p);
        lblLog.setText("Log događaja (poslednjih 8) — „Kopiraj log“ pa mi nalepi u Telegram ako nešto ne radi:");
        logView = tv(root, 11, p);
        logView.setTypeface(android.graphics.Typeface.MONOSPACE);

        setContentView(scroll);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        RecService.logEvent("aplikacija otvorena v" + APP_VERSION + " (servis: "
                + (RecService.instance != null ? "aktivan" : "ne") + ")");
        ui.post(tick);
    }

    @Override protected void onResume() {
        super.onResume();
        applyScreenFlag();
        closeAfterStop = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getBoolean(K_CLOSE_AFTER_STOP, false);
        if (chkClose != null && chkClose.isChecked() != closeAfterStop) chkClose.setChecked(closeAfterStop);
        Recorder r = RecService.recorder;
        String st = r == null ? "nije povezano"
                : (r.isRecording() ? String.format(Locale.US, "SNIMA (%.1f min, %d uzoraka%s)", r.elapsedMs() / 60000,
                        r.writtenSamples(), r.isPaused() ? ", PAUZA" : "")
                : "povezano, ne snima");
        RecService.logEvent("vraćen u aplikaciju — " + st);
        ui.removeCallbacks(tick);
        ui.post(tick);
    }

    @Override protected void onStop() {
        ui.removeCallbacks(tick);      // dok je aplikacija u pozadini ne renderujemo (štedi bateriju);
        getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        super.onStop();                // snimanje nastavlja servis, a onResume odmah osveži prikaz
    }

    /** Drži ekran uključen dok je aplikacija otvorena (snimanje u pozadini ne zavisi od ovoga). */
    private void applyScreenFlag() {
        if (keepAwake) {
            getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    private TextView tv(LinearLayout root, int size, int pad) {
        TextView t = new TextView(this);
        t.setTextSize(size);
        t.setPadding(0, pad / 2, 0, pad / 2);
        root.addView(t);
        return t;
    }

    /** Akcija: prvo direktno u živu instancu servisa; intent samo ako servis još ne postoji. */
    private void act(String action, String label, String toast) {
        clicks++;
        vibrate(60);
        Toast.makeText(this, toast + "  (klik #" + clicks + ")", Toast.LENGTH_SHORT).show();
        Intent i = new Intent(this, RecService.class).setAction(action);
        if (label != null) i.putExtra(RecService.EXTRA_LABEL, label);
        RecService svc = RecService.instance;
        try {
            if (svc != null) {
                RecService.logEvent("klik (direktno): " + action);
                svc.handleAction(action, i);
            } else {
                RecService.logEvent("klik (intent, servis ne postoji): " + action);
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(i); else startService(i);
            }
        } catch (Throwable t) {
            Toast.makeText(this, "Greška: " + t, Toast.LENGTH_LONG).show();
            RecService.logEvent("GRESKA akcije " + action + ": " + t);
        }
    }

    /** Dozvola za rad u pozadini (na Xiaomi/POCO sistem ume da ubije servis bez ovoga). */
    private void batteryDialog() {
        android.os.PowerManager pm =
                (android.os.PowerManager) getSystemService(Context.POWER_SERVICE);
        boolean ok = pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
        RecService.logEvent("baterija: optimizacija " + (ok ? "ISKLJUČENA (dobro)" : "UKLJUČENA (može da prekine snimanje)"));
        if (ok) {
            Toast.makeText(this, "Baterijska optimizacija je već isključena za ovu aplikaciju.", Toast.LENGTH_LONG).show();
            return;
        }
        try {
            Intent i = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            i.setData(Uri.parse("package:" + getPackageName()));
            startActivity(i);
            Toast.makeText(this, "Dozvoli „Bez ograničenja“ / „Don't optimize“.", Toast.LENGTH_LONG).show();
        } catch (Throwable t) {
            try {
                startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + getPackageName())));
            } catch (Throwable t2) {
                Toast.makeText(this, "Ne mogu da otvorim podešavanja: " + t2, Toast.LENGTH_LONG).show();
            }
        }
    }

    private void copyLog() {
        StringBuilder sb = new StringBuilder();
        synchronized (RecService.log) {
            for (String s : RecService.log) sb.append(s).append('\n');
        }
        sb.append("--- stanje: ").append(RecService.status)
          .append(" | greška: ").append(RecService.lastError)
          .append(" | veza: ").append(RecService.recorder != null)
          .append(" | v").append(APP_VERSION);
        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        cm.setPrimaryClip(ClipData.newPlainText("ecg-log", sb.toString()));
        Toast.makeText(this, "Log kopiran u clipboard", Toast.LENGTH_SHORT).show();
    }

    /** Provera da aplikacija uopšte može da piše snimke (folder, prava, prostor). */
    private void testWrite() {
        try {
            File dir = RecService.instance != null
                    ? new File(getExternalFilesDir(null) != null ? getExternalFilesDir(null) : getFilesDir(), "ecg")
                    : new File(getFilesDir(), "ecg");
            if (!dir.exists() && !dir.mkdirs()) throw new Exception("ne mogu da napravim " + dir);
            File f = new File(dir, "test.txt");
            java.io.FileWriter w = new java.io.FileWriter(f, false);
            w.write("test " + System.currentTimeMillis() + "\n");
            w.close();
            String msg = "test upisa OK: " + f.getAbsolutePath() + " (" + f.length() + " B), "
                    + (dir.getFreeSpace() / (1024 * 1024)) + " MB slobodno, " + dir.list().length + " fajlova";
            RecService.logEvent(msg);
            Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
        } catch (Throwable t) {
            RecService.logEvent("test upisa NIJE uspeo: " + t);
            Toast.makeText(this, "Test upisa: " + t, Toast.LENGTH_LONG).show();
        }
    }

    private void vibrate(int ms) {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v == null || !v.hasVibrator()) return;
            if (Build.VERSION.SDK_INT >= 26) v.vibrate(VibrationEffect.createOneShot(ms, 128));
            else v.vibrate(ms);
        } catch (Throwable ignored) {}
    }

    private Button btn(String text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(13);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, 0, (int) (6 * getResources().getDisplayMetrics().density), 0);
        b.setLayoutParams(lp);
        return b;
    }

    private LinearLayout row(View... bs) {
        LinearLayout r = new LinearLayout(this);
        r.setOrientation(LinearLayout.HORIZONTAL);
        for (View b : bs) r.addView(b);
        return r;
    }

    /** Posle „Stop“: sačekaj da servis zaista završi upis, pa zatvori aplikaciju (ako je opcija uključena).
     *  Ako snimak NIJE sačuvan, aplikacija ostaje otvorena (da se vidi greška i proba ponovo). */
    private void waitThenClose(final int tries) {
        ui.postDelayed(() -> {
            Recorder r = RecService.recorder;
            if (r != null && r.isRecording() && tries < 40) { waitThenClose(tries + 1); return; }
            String st = RecService.status == null ? "" : RecService.status;
            if (st.startsWith("snimak sačuvan")) {
                Toast.makeText(this, "Snimak sačuvan — zatvaram aplikaciju", Toast.LENGTH_LONG).show();
                RecService.logEvent("gašenje aplikacije posle snimanja (opcija uključena)");
                closeApp();
            } else {
                RecService.logEvent("gašenje preskočeno (snimak nije potvrđen): " + st);
                Toast.makeText(this, "Ne zatvaram — " + st, Toast.LENGTH_LONG).show();
            }
        }, 600);
    }

    /** Prekini vezu (oslobodi USB port i zaustavi servis) pa zatvori aktivnost. */
    private void closeApp() {
        try {
            RecService svc = RecService.instance;
            Intent i = new Intent(this, RecService.class).setAction(RecService.ACTION_DISCONNECT);
            if (svc != null) svc.handleAction(RecService.ACTION_DISCONNECT, i);
            else if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
            else startService(i);
        } catch (Throwable t) {
            RecService.logEvent("gašenje: " + t);
        }
        ui.postDelayed(() -> {
            try {
                ui.removeCallbacks(tick);
                if (Build.VERSION.SDK_INT >= 21) finishAndRemoveTask(); else finish();
            } catch (Throwable t) {
                finish();
            }
        }, 500);
    }

    private void upload() {
        File f = lastFile;
        Recorder r = RecService.recorder;
        if (r != null && r.isRecording()) f = r.file();
        if (f == null || !f.exists()) {
            Toast.makeText(this, "nema fajla za slanje", Toast.LENGTH_SHORT).show();
            RecService.logEvent("slanje odbijeno: nema fajla");
            return;
        }
        if (r != null && r.isRecording()) {
            Toast.makeText(this, "prvo zaustavi snimanje (Stop)", Toast.LENGTH_SHORT).show();
            return;
        }
        final File file = f;
        uploadView.setText("šaljem " + file.getName() + " (" + (file.length() / 1024) + " KB)…");
        RecService.logEvent("slanje: " + file.getName());
        new Thread(() -> {
            String out;
            try {
                out = Uploader.upload(file, 192, "android-app");
                try { Thread.sleep(4000); } catch (InterruptedException ignored) {}
                out += "\nstatus: " + Uploader.status().trim();
            } catch (Exception e) {
                out = "greška: " + e.getMessage();
            }
            final String msg = out;
            RecService.logEvent("slanje: " + msg.replace('\n', ' '));
            ui.post(() -> uploadView.setText(msg));
        }).start();
    }
}
