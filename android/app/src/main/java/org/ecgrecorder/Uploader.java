package org.ecgrecorder;

import java.io.BufferedReader;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Slanje snimka na ECG servis (example.com/ecg/api.php?action=submit)
 * i provera statusa posla.
 *
 * Cloudflare propušta samo klijente sa pravim User-Agent-om (podrazumevani
 * "Java/…" ume da bude odbijen), pa se UA postavlja eksplicitno.
 */
public class Uploader {

    public static final String BASE = "https://example.com/ecg/api.php";
    private static final String UA = "ecg-app/1.0 (+android)";
    private static final String CRLF = "\r\n";

    public static String upload(File csv, int fs, String source) throws IOException {
        String boundary = "----ecgapp" + System.currentTimeMillis();
        HttpURLConnection c = (HttpURLConnection) new URL(BASE + "?action=submit").openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setConnectTimeout(20000);
        c.setReadTimeout(120000);
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
        try (DataOutputStream o = new DataOutputStream(c.getOutputStream())) {
            writeField(o, boundary, "fs", String.valueOf(fs));
            writeField(o, boundary, "source", source);
            o.writeBytes("--" + boundary + CRLF);
            o.writeBytes("Content-Disposition: form-data; name=\"data_file\"; filename=\"" + csv.getName() + "\"" + CRLF);
            o.writeBytes("Content-Type: text/csv" + CRLF + CRLF);
            try (FileInputStream in = new FileInputStream(csv)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
            }
            o.writeBytes(CRLF + "--" + boundary + "--" + CRLF);
            o.flush();
        }
        int code = c.getResponseCode();
        String body = read(code >= 400 ? c.getErrorStream() : c.getInputStream());
        if (code >= 400) throw new IOException("HTTP " + code + ": " + body);
        return body;
    }

    public static String status() throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(BASE + "?action=status").openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(30000);
        c.setRequestProperty("User-Agent", UA);
        return read(c.getInputStream());
    }

    private static void writeField(DataOutputStream o, String boundary, String name, String value)
            throws IOException {
        o.writeBytes("--" + boundary + CRLF);
        o.writeBytes("Content-Disposition: form-data; name=\"" + name + "\"" + CRLF + CRLF);
        o.writeBytes(value + CRLF);
    }

    private static String read(InputStream in) throws IOException {
        if (in == null) return "";
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append('\n');
        }
        return sb.toString();
    }
}
