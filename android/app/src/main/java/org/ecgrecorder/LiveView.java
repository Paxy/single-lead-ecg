package org.ecgrecorder;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.util.AttributeSet;
import android.view.View;

import java.util.List;

/**
 * Živi prikaz ECG talasa (poslednjih ~5 s) sa automatskom skalom.
 * Služi da se odmah vidi da li su elektrode dobro postavljene:
 * ravan crta = nema signala, ogromni skokovi = pokret/loš kontakt, čist periodičan
 * talas = kontakt je dobar (vidi se R talas i T talas).
 */
public class LiveView extends View {

    private final Paint grid = new Paint();
    private final Paint axis = new Paint();
    private final Paint trace = new Paint();
    private final Paint text = new Paint();
    private final Paint band = new Paint();
    private Path path = new Path();
    private List<double[]> data;
    private int pp;
    private int contact = Recorder.CONTACT_WEAK;
    private String label = "";
    private long beatAge = -1;
    private final Paint beatDot = new Paint();

    public LiveView(Context c, AttributeSet a) {
        super(c, a);
        grid.setColor(0xFF1E2A36);
        grid.setStrokeWidth(1f);
        axis.setColor(0xFF3A4B5C);
        axis.setStrokeWidth(2f);
        trace.setColor(0xFF39D98A);
        trace.setStrokeWidth(3f);
        trace.setStyle(Paint.Style.STROKE);
        trace.setAntiAlias(true);
        text.setColor(0xFFE8EEF5);
        text.setTextSize(28f);
        text.setAntiAlias(true);
        beatDot.setColor(0xFFFF3B30);
        beatDot.setAntiAlias(true);
        band.setColor(0x332F6FEB);
        setBackgroundColor(0xFF0B1219);
    }

    /** Podaci za crtanje: niz [tMs, v]; pp i stanje kontakta dolaze iz Recorder-a.
     *  `beatAgeMs` = koliko ms od poslednjeg otkucaja (-1 ako još nema) — koristi se za blink. */
    public void update(List<double[]> trace, int pp, int contact, String label, long beatAgeMs) {
        this.data = trace;
        this.pp = pp;
        this.contact = contact;
        this.label = label == null ? "" : label;
        this.beatAge = beatAgeMs;
        invalidate();
    }

    @Override protected void onDraw(Canvas c) {
        super.onDraw(c);
        int w = getWidth(), h = getHeight();
        // mreža
        for (int i = 1; i < 10; i++) c.drawLine(w * i / 10f, 0, w * i / 10f, h, grid);
        for (int i = 1; i < 6; i++) c.drawLine(0, h * i / 6f, w, h * i / 6f, grid);

        if (data == null || data.size() < 10) {
            c.drawText("čekam podatke sa uređaja…", 20, h / 2f, text);
            return;
        }
        double mn = Double.MAX_VALUE, mx = -Double.MAX_VALUE;
        long firstT = (long) data.get(0)[0], lastT = (long) data.get(data.size() - 1)[0];
        for (double[] p : data) { if (p[1] < mn) mn = p[1]; if (p[1] > mx) mx = p[1]; }
        double span = Math.max(60, mx - mn);
        double mid = (mn + mx) / 2;
        double lo = mid - span * 0.75, hi = mid + span * 0.75;
        double tSpan = Math.max(1000, lastT - firstT);

        // traka za loš signal (ravna linija / saturacija / šum-pokret)
        if (contact == Recorder.CONTACT_FLAT || contact == Recorder.CONTACT_SAT) {
            band.setColor(0x332F6FEB);
            c.drawRect(0, 0, w, h, band);
        } else if (contact == Recorder.CONTACT_NOISE) {
            band.setColor(0x33FF9800);
            c.drawRect(0, 0, w, h, band);
        }
        c.drawLine(0, (float) (h * (hi - mid) / (hi - lo)), w,
                (float) (h * (hi - mid) / (hi - lo)), axis);

        path = new Path();
        boolean started = false;
        for (double[] p : data) {
            float x = (float) (w * (p[0] - firstT) / tSpan);
            float y = (float) (h * (hi - p[1]) / (hi - lo));
            if (!started) { path.moveTo(x, y); started = true; } else path.lineTo(x, y);
        }
        c.drawPath(path, trace);

        c.drawText(String.format(java.util.Locale.US, "pp %d   %s", pp,
                contact == Recorder.CONTACT_OK ? "kontakt DOBAR"
                        : contact == Recorder.CONTACT_WEAK ? "kontakt SLAB"
                        : contact == Recorder.CONTACT_FLAT ? "ELEKTRODA OTPALA"
                        : contact == Recorder.CONTACT_NOISE ? "ŠUM/POKRET (ignorišem)" : "SATURACIJA"),
                16, 34, text);
        // blink crvena tačka na svaki otkucaj (dokaz da signal živi); gasi se posle 250 ms
        if (beatAge >= 0 && beatAge < 250) {
            c.drawCircle(w - 40, 40, 18, beatDot);
        } else if (beatAge >= 0) {
            beatDot.setColor(0x55FF3B30);
            c.drawCircle(w - 40, 40, 10, beatDot);
            beatDot.setColor(0xFFFF3B30);
        }
        c.drawText(label, 16, h - 16, text);
    }
}
