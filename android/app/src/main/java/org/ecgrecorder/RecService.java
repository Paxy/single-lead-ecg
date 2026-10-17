package org.ecgrecorder;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;
import android.widget.Toast;

import com.hoho.android.usbserial.driver.UsbSerialDriver;
import com.hoho.android.usbserial.driver.UsbSerialPort;
import com.hoho.android.usbserial.driver.UsbSerialProber;
import com.hoho.android.usbserial.util.SerialInputOutputManager;

import java.io.File;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Veza sa USB uređajem i snimanje, u foreground servisu (radi i sa ugašenim ekranom).
 *
 * Akcije: CONNECT / DISCONNECT / REC_START / REC_PAUSE_TOGGLE / REC_STOP / AUTO_TOGGLE /
 * MARK / RESET.
 *
 * VAŽNO (naučeno na v1.4): pozadinska nit NE SME da umre. Ranije je svaki `bgHandler.post`
 * bez try/catch mogao da ubije nit (npr. izuzetak u ioManager-u ili u osvežavanju
 * notifikacije), a tada svi sledeći klikovi tiho ne rade ništa i `status` ostane zaglavljen
 * na „SNIMAM…". Zato sve ide preko `runOnBg`/`runOnBgDelayed` (try/catch + provera da li nit
 * živi, uz ponovno podizanje), a `reconcile()` svaki put uskladi status sa STVARNIM stanjem.
 */
public class RecService extends Service {

    public static final String ACTION_CONNECT = "org.ecgrecorder.CONNECT";
    public static final String ACTION_DISCONNECT = "org.ecgrecorder.DISCONNECT";
    public static final String ACTION_REC_START = "org.ecgrecorder.REC_START";
    public static final String ACTION_REC_PAUSE_TOGGLE = "org.ecgrecorder.REC_PAUSE_TOGGLE";
    public static final String ACTION_REC_STOP = "org.ecgrecorder.REC_STOP";
    public static final String ACTION_AUTO_TOGGLE = "org.ecgrecorder.AUTO_TOGGLE";
    public static final String ACTION_MARK = "org.ecgrecorder.MARK";
    public static final String ACTION_RESET = "org.ecgrecorder.RESET";
    public static final String ACTION_SENS_TOGGLE = "org.ecgrecorder.SENS_TOGGLE";
    public static final String EXTRA_LABEL = "label";

    private static final String ACTION_USB_PERMISSION = "org.ecgrecorder.USB_PERMISSION";
    private static final String TAG = "EcgRecService";
    private static final int NOTIF_ID = 4711;
    private static final String CHANNEL = "ecg_rec";

    public static volatile RecService instance;
    public static volatile Recorder recorder;
    public static volatile String status = "nije povezano";
    public static volatile String lastError = "";
    public static volatile int reconnects = 0;

    /** Log događaja: na ekranu (poslednjih 40) + u fajlu. */
    public static final java.util.ArrayDeque<String> log = new java.util.ArrayDeque<>();
    private static File fileLog;

    public static void logEvent(String msg) {
        String line = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date()) + "  " + msg;
        synchronized (log) {
            log.addLast(line);
            while (log.size() > 40) log.removeFirst();
        }
        if (fileLog != null) {
            try {
                if (fileLog.length() > 200_000) {
                    File old = new File(fileLog.getAbsolutePath() + ".1");
                    if (old.exists()) old.delete();
                    fileLog.renameTo(old);
                }
                java.io.FileWriter w = new java.io.FileWriter(fileLog, true);
                w.write(line + "\n");
                w.close();
            } catch (Throwable ignored) {}
        }
    }

    private UsbManager usbManager;
    private UsbSerialPort port;
    private UsbDeviceConnection connection;
    private SerialInputOutputManager ioManager;
    private HandlerThread bgThread;
    private Handler bgHandler;
    private PowerManager.WakeLock wakeLock;
    private UsbDevice pendingDevice;
    private boolean wantConnected = false;
    private int fgsType;

    // ------------------------------------------------------------------ nit
    private void ensureBg() {
        if (bgThread == null || !bgThread.isAlive()) {
            bgThread = new HandlerThread("ecg-usb");
            bgThread.start();
            bgHandler = new Handler(bgThread.getLooper());
            logEvent("pozadinska nit podignuta");
        }
    }

    private void runOnBg(Runnable r) {
        ensureBg();
        bgHandler.post(() -> {
            try {
                r.run();
            } catch (Throwable t) {
                lastError = "pozadina: " + t;
                logEvent("GRESKA u pozadini: " + t);
                Log.e(TAG, "greška u pozadinskom poslu", t);
            }
        });
    }

    private void runOnBgDelayed(Runnable r, long ms) {
        ensureBg();
        bgHandler.postDelayed(() -> {
            try {
                r.run();
            } catch (Throwable t) {
                lastError = "pozadina: " + t;
                logEvent("GRESKA u pozadini: " + t);
                Log.e(TAG, "greška u pozadinskom poslu", t);
            }
        }, ms);
    }

    /** Status se NIKAD ne izvodi iz sećanja — uvek iz stvarnog stanja. */
    private void reconcile() {
        Recorder r = recorder;
        if (r == null) {
            String s = status == null ? "" : status;
            if (s.startsWith("SNIMAM") || s.startsWith("PAUZA")) status = "nije povezano";
        } else if (r.isRecording()) {
            status = (r.isPaused() ? "PAUZA (" + r.pauseReason() + ")"
                    : "SNIMAM — " + (r.file() == null ? "" : r.file().getName()));
        } else {
            String s = status == null ? "" : status;
            if (!s.startsWith("snimak sačuvan") && !s.startsWith("resetovano")) {
                status = "povezano — gledam talas";
            }
        }
    }

    // -------------------------------------------------------------- receiver
    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context ctx, Intent intent) {
            try {
                String a = intent.getAction();
                if (ACTION_USB_PERMISSION.equals(a)) {
                    UsbDevice dev = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
                    boolean ok = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false);
                    if (ok && dev != null) {
                        pendingDevice = dev;
                        logEvent("USB dozvola data: " + describe(dev));
                        runOnBg(RecService.this::openPort);
                    } else {
                        lastError = "USB dozvola nije data";
                        logEvent(lastError);
                    }
                } else if (UsbManager.ACTION_USB_DEVICE_DETACHED.equals(a)) {
                    logEvent("uređaj odvojen (USB)");
                    if (recorder != null) runOnBg(RecService.this::scheduleReconnect);
                }
            } catch (Throwable t) {
                logEvent("GRESKA u receiveru: " + t);
            }
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        instance = this;
        fileLog = new File(getFilesDir(), "service.log");
        logEvent("servis kreiran (v" + MainActivity.APP_VERSION + ")");
        usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);
        ensureBg();
        createChannel();
        IntentFilter f = new IntentFilter();
        f.addAction(ACTION_USB_PERMISSION);
        f.addAction(UsbManager.ACTION_USB_DEVICE_DETACHED);
        // RECEIVER_EXPORTED je obavezan na API 33+: USB dozvola stiže od SystemUI-ja (drugi UID)
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(usbReceiver, f, Context.RECEIVER_EXPORTED);
        else registerReceiver(usbReceiver, f);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        try {
            startForegroundSafely();
        } catch (Throwable t) {
            logEvent("foreground: " + t);
        }
        if (action == null) {
            logEvent("servis ponovo pokrenut bez akcije (sistem)");
            reconcile();
            return START_STICKY;
        }
        handleAction(action, intent);
        return START_STICKY;
    }

    /** Ulazna tačka — poziva je i onStartCommand i Activity direktno (isti proces). */
    public void handleAction(String action, Intent intent) {
        try {
            logEvent("akcija: " + action + (RecService.instance == this ? "" : " (intent)"));
            switch (action) {
                case ACTION_CONNECT:
                    wantConnected = true;
                    runOnBg(this::requestPermissionOrOpen);
                    break;
                case ACTION_DISCONNECT:
                    wantConnected = false;
                    runOnBg(() -> {
                        if (recorder != null && recorder.isRecording()) recorder.endFile();
                        closePort();
                        recorder = null;
                        releaseWake();
                        status = "veza prekinuta";
                        logEvent("veza prekinuta");
                        cancelNotifTick();
                        stopSelf();
                    });
                    break;
                case ACTION_REC_START:
                    runOnBg(() -> {
                        Recorder r = recorder;
                        if (r == null) {
                            lastError = "nije povezano — prvo „Poveži uređaj“";
                            logEvent("start ODBIJEN: " + lastError);
                            return;
                        }
                        if (r.isRecording()) { logEvent("start: već snima"); return; }
                        String name = "ECG_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
                                .format(new Date()) + ".csv";
                        File dir = recDir();
                        if (r.beginFile(dir, name)) {
                            acquireWake();
                            status = "SNIMAM — " + name;
                            logEvent("start OK: " + new File(dir, name).getAbsolutePath());
                            cancelNotifTick();
                            bgHandler.post(notifTick);
                        } else {
                            logEvent("start NIJE uspeo: " + (lastError.isEmpty() ? "nepoznat razlog" : lastError));
                        }
                    });
                    break;
                case ACTION_REC_PAUSE_TOGGLE:
                    runOnBg(() -> {
                        Recorder r = recorder;
                        if (r == null || !r.isRecording()) { logEvent("pauza: ne snima se"); return; }
                        r.togglePause();
                        logEvent(r.isPaused() ? "ručna pauza" : "nastavljeno");
                    });
                    break;
                case ACTION_REC_STOP:
                    runOnBg(() -> {
                        Recorder r = recorder;
                        if (r == null) return;
                        if (r.isRecording()) {
                            String f = r.file() == null ? "-" : r.file().getName();
                            String stat = String.format(Locale.US, "%.1f min, %d uzoraka, %d KB",
                                    r.elapsedMs() / 60000, r.writtenSamples(),
                                    r.file() == null ? 0 : r.file().length() / 1024);
                            r.endFile();
                            status = "snimak sačuvan: " + f + " (" + stat + ")";
                            logEvent("stop: " + f + " (" + stat + ")");
                            cancelNotifTick();
                            releaseWake();
                            notifyNow("Snimak sačuvan: " + f);
                        } else {
                            logEvent("stop: ništa se ne snima");
                        }
                    });
                    break;
                case ACTION_AUTO_TOGGLE:
                    Recorder r = recorder;
                    if (r != null) {
                        r.setAutoPause(!r.isAutoPause());
                        logEvent("auto-pauza: " + (r.isAutoPause() ? "UKLJ" : "ISKLJ"));
                    } else {
                        logEvent("auto-pauza: nije povezano");
                    }
                    break;
                case ACTION_MARK:
                    String label = intent == null ? null : intent.getStringExtra(EXTRA_LABEL);
                    Recorder rm = recorder;
                    if (rm == null) logEvent("marker: nije povezano");
                    else if (!rm.isRecording()) logEvent("marker: ne snima se");
                    else { rm.mark(label); logEvent("marker: " + label); }
                    break;
                case ACTION_SENS_TOGGLE:
                    Recorder rs = recorder;
                    if (rs != null) rs.cycleSensitivity();
                    else logEvent("osetljivost: nije povezano");
                    break;
                case ACTION_RESET:
                    runOnBg(() -> {
                        if (recorder != null && recorder.isRecording()) recorder.endFile();
                        closePort();
                        recorder = null;
                        wantConnected = false;
                        releaseWake();
                        cancelNotifTick();
                        status = "resetovano — pritisni „Poveži uređaj“";
                        lastError = "";
                        logEvent("RESET stanja (veza zatvorena, snimanje zaustavljeno ako je bilo)");
                        stopSelf();
                    });
                    break;
                default:
                    logEvent("nepoznata akcija: " + action);
            }
            reconcile();
        } catch (Throwable t) {
            lastError = action + ": " + t;
            logEvent("GRESKA " + action + ": " + t);
            Log.e(TAG, "greška u akciji " + action, t);
        }
    }

    private File recDir() {
        File d = getExternalFilesDir(null);
        if (d == null) d = getFilesDir();
        return new File(d, "ecg");
    }

    // ------------------------------------------------------------ notifikacija
    private final Runnable notifTick = new Runnable() {
        @Override public void run() {
            try {
                Recorder r = recorder;
                if (r != null && r.isRecording()) {
                    notifyNow(String.format(Locale.US, "SNIMAM %s · %d uzoraka · puls %s%s",
                            hms(r.elapsedMs()), r.writtenSamples(), r.liveHr(),
                            r.isPaused() ? " · PAUZA (" + r.pauseReason() + ")" : ""));
                    runOnBgDelayed(this, 2000);
                } else {
                    notifyNow(r != null ? "Povezano — gledam talas" : status);
                }
            } catch (Throwable t) {
                logEvent("notifikacija: " + t);
            }
        }
    };

    private void cancelNotifTick() {
        try {
            if (bgHandler != null) bgHandler.removeCallbacks(notifTick);
        } catch (Throwable ignored) {}
    }

    private static String hms(double ms) {
        long s = (long) (ms / 1000);
        return String.format(Locale.US, "%02d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60);
    }

    private void createChannel() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(CHANNEL, "ECG snimanje",
                NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("Snimanje sa USB ECG uređaja");
        nm.createNotificationChannel(ch);
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, RecService.class).setAction(ACTION_REC_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle("ECG Recorder")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.presence_video_online)
                .setContentIntent(pi)
                .addAction(new Notification.Action.Builder(null, "Stop snimanja", stopPi).build())
                .setOngoing(true)
                .build();
    }

    private void notifyNow(String text) {
        try {
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.notify(NOTIF_ID, buildNotification(text));
        } catch (Throwable t) {
            logEvent("notifikacija nije uspela: " + t);
        }
    }

    private void startForegroundSafely() {
        Notification n = buildNotification(status);
        if (Build.VERSION.SDK_INT >= 29) {
            int[] types = {android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
                           android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC};
            for (int t : types) {
                try {
                    startForeground(NOTIF_ID, n, t);
                    fgsType = t;
                    return;
                } catch (Throwable e) {
                    Log.w(TAG, "startForeground tip " + t + " nije prošao: " + e);
                }
            }
            logEvent("foreground nije dozvoljen (oba tipa odbijena)");
            try { startForeground(NOTIF_ID, n); } catch (Throwable ignored) {}
        } else {
            try { startForeground(NOTIF_ID, n); } catch (Throwable ignored) {}
        }
    }

    @Override public void onTaskRemoved(Intent rootIntent) {
        logEvent("aplikacija izbačena iz skora — snimanje nastavlja u pozadini");
        super.onTaskRemoved(rootIntent);
    }

    // ------------------------------------------------------------------- USB
    private static String describe(UsbDevice d) {
        return String.format(Locale.US, "%04x:%04x", d.getVendorId(), d.getProductId());
    }

    private void requestPermissionOrOpen() {
        List<UsbSerialDriver> drivers = UsbSerialProber.getDefaultProber().findAllDrivers(usbManager);
        if (drivers.isEmpty()) {
            lastError = "nema USB serijskog uređaja (proveri OTG i da je modul priključen)";
            logEvent(lastError);
            return;
        }
        UsbDevice dev = drivers.get(0).getDevice();
        if (!usbManager.hasPermission(dev)) {
            pendingDevice = dev;
            logEvent("tražim USB dozvolu za " + describe(dev));
            PendingIntent pi = PendingIntent.getBroadcast(this, 0,
                    new Intent(ACTION_USB_PERMISSION).setPackage(getPackageName()),
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
            usbManager.requestPermission(dev, pi);
            return;
        }
        openPort();
    }

    private void openPort() {
        if (recorder != null) { logEvent("već povezano"); return; }
        if (!wantConnected) { logEvent("openPort preskočen (veza nije tražena)"); return; }
        List<UsbSerialDriver> drivers = UsbSerialProber.getDefaultProber().findAllDrivers(usbManager);
        if (drivers.isEmpty()) { lastError = "uređaj nije nađen"; logEvent(lastError); return; }
        UsbSerialDriver drv = null;
        for (UsbSerialDriver d : drivers) {
            if (pendingDevice == null || d.getDevice().equals(pendingDevice)) { drv = d; break; }
        }
        if (drv == null) drv = drivers.get(0);
        if (drv.getPorts().isEmpty()) { lastError = "uređaj nema serijski port"; logEvent(lastError); return; }

        connection = usbManager.openDevice(drv.getDevice());
        if (connection == null) { lastError = "openDevice vraća null (dozvola?)"; logEvent(lastError); return; }

        port = drv.getPorts().get(0);
        try {
            port.open(connection);
            port.setParameters(9600, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE);
        } catch (IOException e) {
            lastError = "otvaranje porta: " + e.getMessage();
            logEvent(lastError);
            closePort();
            return;
        }

        Recorder r = new Recorder(msg -> { lastError = msg; logEvent(msg); });
        Recorder.setLogger(RecService::logEvent);
        recorder = r;
        ioManager = new SerialInputOutputManager(port, new SerialInputOutputManager.Listener() {
            @Override public void onNewData(byte[] data) {
                try {
                    Recorder rec = recorder;
                    if (rec != null) rec.onData(data);
                } catch (Throwable t) {
                    logEvent("greška u čitanju: " + t);
                }
            }
            @Override public void onRunError(Exception e) {
                try {
                    lastError = "veza prekinuta: " + e.getMessage();
                    logEvent(lastError);
                    scheduleReconnect();
                } catch (Throwable t) {
                    logEvent("greška u reconnect-u: " + t);
                }
            }
        });
        // ioManager se vrti u SVOJOJ niti — greška u njemu ne sme da ubije bg nit servisa
        try {
            new Thread(ioManager, "ecg-io").start();
        } catch (Throwable t) {
            lastError = "ne mogu da pokrenem čitanje: " + t;
            logEvent(lastError);
            closePort();
            recorder = null;
            return;
        }
        acquireWake();
        reconnects = 0;
        status = "povezano — gledam talas (" + describe(drv.getDevice()) + ")";
        logEvent("povezano: " + describe(drv.getDevice()) + " @9600 baud");
        notifyNow(status);
        reconcile();
    }

    private void scheduleReconnect() {
        Recorder r = recorder;
        boolean wasRecording = r != null && r.isRecording();
        if (wasRecording) r.endFile();
        closePort();
        recorder = null;
        releaseWake();
        cancelNotifTick();
        if (!wantConnected || reconnects >= 20) {
            status = wasRecording ? "veza prekinuta — snimak sačuvan" : "veza prekinuta";
            logEvent(status);
            return;
        }
        reconnects++;
        final int n = reconnects;
        status = "pokušavam ponovo da se povežem (" + n + "/20)…";
        logEvent(status);
        runOnBgDelayed(this::openPort, 2000);
    }

    private void acquireWake() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) return;
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ecgrecorder:rec");
            wakeLock.acquire(6 * 60 * 60 * 1000L);
        } catch (Throwable t) {
            logEvent("wake lock: " + t);
        }
    }

    private void releaseWake() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Throwable ignored) {}
    }

    private void closePort() {
        try {
            if (ioManager != null) { ioManager.stop(); ioManager = null; }
        } catch (Throwable ignored) {}
        try {
            if (port != null) { port.close(); port = null; }
        } catch (Throwable ignored) {}
        try {
            if (connection != null) { connection.close(); connection = null; }
        } catch (Throwable ignored) {}
    }

    @Override public void onDestroy() {
        try { unregisterReceiver(usbReceiver); } catch (Throwable ignored) {}
        Recorder r = recorder;
        if (r != null && r.isRecording()) r.endFile();
        closePort();
        releaseWake();
        cancelNotifTick();
        if (bgThread != null) bgThread.quitSafely();
        if (instance == this) instance = null;
        logEvent("servis zaustavljen");
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
