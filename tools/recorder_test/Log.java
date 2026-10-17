package android.util;

/** Stub za android.util.Log — da se Recorder.java može kompajlirati i testirati van Androida. */
public final class Log {
    public static int v(String t, String m) { return 0; }
    public static int d(String t, String m) { return 0; }
    public static int i(String t, String m) { return 0; }
    public static int w(String t, String m) { System.out.println("W/" + t + ": " + m); return 0; }
    public static int e(String t, String m) { System.out.println("E/" + t + ": " + m); return 0; }
    public static int e(String t, String m, Throwable x) { System.out.println("E/" + t + ": " + m + " " + x); return 0; }
}
