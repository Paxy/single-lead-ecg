# Deployment

Target: a plain LAMP box (tested on Debian/Ubuntu with Apache 2.4 and php-fpm 8.4).
No Docker, no framework — this is deliberately small so it can be understood end to end.

## 1. Packages

```bash
apt install apache2 libapache2-mod-fcgid php8.4-fpm php8.4-cli
a2enmod ssl headers rewrite proxy_fcgi
python3 -m venv /opt/ecg-venv
/opt/ecg-venv/bin/pip install numpy scipy pandas matplotlib neurokit2
```

`report.sh` prefers `/opt/ecg-venv/bin/python3`; set `ECG_PYTHON` to override.

## 2. Files

```bash
mkdir -p /var/www/html/ecg/data
cp web/*.php web/*.html /var/www/html/ecg/
cp web/ecg_analyze.py web/ecg_medical.py web/ecg_segments.py web/report.sh /var/www/html/ecg/
chown -R www-data:www-data /var/www/html/ecg/data
chmod 775 /var/www/html/ecg/data
chmod 755 /var/www/html/ecg/report.sh
chown root:root /var/www/html/ecg/*.php /var/www/html/ecg/*.py
```

`data/` must be writable by the web user (CSV upload, JSON, PNG, history) and must **not** be
served as static files if the recordings are private — put the vhost document root at
`/var/www/html` (not `.../ecg/data`) and, if needed, deny `data/` explicitly:

```apache
<Directory /var/www/html/ecg/data>
    Require all denied
</Directory>
```

## 3. PHP limits

The host runs **php-fpm**, so `php_value` in the vhost or `.htaccess` is ignored
(`AllowOverride None`). Put the limits in `web/.user.ini` (shipped) inside the service
directory:

```ini
upload_max_filesize = 64M
post_max_size = 70M
max_execution_time = 120
memory_limit = 512M
```

Check it through the public URL: a small PHP file printing `php_sapi_name()` and
`ini_get("upload_max_filesize")` must show `fpm-fcgi|64M`. If it shows `2M`, the `.user.ini`
cache (300 s) has not expired or the file is in the wrong directory.

## 4. vhost sketch

```apache
<VirtualHost *:443>
    ServerName ecg.example.com
    DocumentRoot /var/www/html/ecg
    SSLEngine on
    SSLCertificateFile      /etc/letsencrypt/live/ecg.example.com/fullchain.pem
    SSLCertificateKeyFile   /etc/letsencrypt/live/ecg.example.com/privkey.pem
    <FilesMatch \.php$>
        SetHandler "proxy:unix:/run/php/php8.4-fpm.sock|fcgi://localhost"
    </FilesMatch>
    Header always set X-Content-Type-Options nosniff
</VirtualHost>
```

HTTPS is **mandatory** for Web Serial: the browser refuses serial access on a plain-HTTP page.

## 5. First run

1. Open `/` — an empty dashboard is fine.
2. Open `/plotter.html` on the machine the device is plugged into, click
   **Simulator (no hardware)**, let it run 20 s, then **Analyse on server**.
   The result card must show a heart rate close to the simulator's.
3. Or via CLI (does not touch `data/`):

```bash
cd /var/www/html/ecg
./report.sh /path/to/recording.csv 192          # writes to ./data
```

## 6. Background job mechanics

`api.php?action=submit` does not analyse in the request:

```php
file_put_contents("$DATA/job.json", json_encode(["id" => $id, "state" => "running", …]));
exec("setsid nohup $PY $DIR/ecg_analyze.py --input … --outdir … --job-json $DATA/job.json > /dev/null 2>&1 &");
```

The client polls `?action=status`. A `flock` on `data/.analysis.lock` keeps it single-file.
Reason: reverse proxies (Cloudflare, nginx) cut requests around 100 s, and a long recording
takes longer than that.

Cloudflare note: the default Python `User-Agent` is refused (`403 error code: 1010`), so any
client that posts recordings must set a real UA (the Android app sends `ecg-app/1.0`).

## 7. Updating the engine

Keep the previous file: bugs here are silent, and a bad deploy can overwrite a valid report.

```bash
cd /var/www/html/ecg
ts=$(date +%Y%m%d-%H%M)
cp -a ecg_analyze.py ecg_analyze.py.bak-$ts
cp -a ecg_medical.py ecg_medical.py.bak-$ts
install -m 644 /tmp/ecg_analyze.py /tmp/ecg_medical.py /tmp/ecg_segments.py .
/opt/ecg-venv/bin/python3 -m py_compile ecg_analyze.py ecg_medical.py ecg_segments.py
php -l index.php && php -l api.php
```

Then run the regression set (synthetic + a few real recordings) into a **separate** outdir and
compare HR/PR/QRS/QT before deciding to keep the change.

## 8. Re-analysing the archive after an engine fix

Recordings and their old reports live in `data/history/<id>/`. To re-run them:

```bash
for d in data/history/*/; do
  id=$(basename "$d")
  /opt/ecg-venv/bin/python3 ecg_analyze.py --input "$d/data.csv" \
      --outdir /tmp/rean/$id --prefix data --fs 192 --source androidapp
  cp -a "$d/data.txt"  "$d/data.txt.pre-fix"   # only the first time
  cp -a "$d/data.json" "$d/data.json.pre-fix"
  install -o www-data -g www-data -m 664 /tmp/rean/$id/data.txt  "$d/data.txt"
  install -o www-data -g www-data -m 664 /tmp/rean/$id/data.json "$d/data.json"
done
```

Always keep `data.csv` untouched, and never restore a whole `data/` backup without checking
`mtime` first — a new recording can arrive between backup and restore.

## 9. Restricting the service to the local network

A dashboard that shows the **latest recording** displays personal health data, so the natural
deployment is LAN-only. Apache evaluates the *real* client address (behind Cloudflare,
`mod_remoteip` + `RemoteIPHeader CF-Connecting-IP` rewrites it), so a single directory block is
enough — traffic that arrives through the CDN carries the client's public address and is refused:

```apache
# /etc/apache2/conf-available/ecg-local-only.conf  (a2enconf ecg-local-only)
<Directory "/var/www/html/services/ecg">
    Require ip 172.16.0.0/12 10.0.0.0/8 192.168.0.0/16 127.0.0.1 ::1                 <your-home-public-ipv4> <your-home-ipv6-prefix>::/64
    ErrorDocument 403 "ECG service is available from the local network only."
</Directory>
```

Why the home public addresses are listed as well: a client on the LAN may still resolve the
hostname to the CDN (IPv6 AAAA record, DoH on a phone), and in that case the address Apache sees
is the home egress address, not a private one. Listing the home IPv4 and the IPv6 prefix keeps
that path working without opening the service to the internet.

Add a **local DNS override** (A record → server's LAN address) on the LAN resolver as well, so
devices take the direct path and keep using the valid certificate (Web Serial needs HTTPS).
Verify from both sides:

```bash
# from the LAN — must be 200
curl -sk --resolve ecg.example.com:443:192.168.1.10 https://ecg.example.com/ecg/index.php
# from outside — must be 403 (a public proxy/status checker is enough)
curl -s -o /dev/null -w '%{http_code}\n' https://ecg.example.com/ecg/index.php
```

Consequence worth knowing: the Android app uploads to the same hostname, so an upload made on
mobile data will be refused by design — upload once back on the home network.

## 10. Diagnostics endpoints

`web/diag.php` + `web/serial_diag.html` are a browser/USB diagnostic pair: they report whether
the page runs in a WebView, the Chrome version, `navigator.serial`/`usb` availability and then
test-read the port for 5 s, appending the result to `data/serial_diag.log`.
Useful while setting up a phone, but `diag.php` is an unauthenticated endpoint that writes to
disk — remove both files (or restrict them by IP) on a public deployment.

## 11. Housekeeping

- `data/history/` is pruned to the last N sessions by the engine (`archive_session(keep=…)`).
- `data/analysis.log` grows; rotate it (logrotate or a cron `truncate`).
- The plotter's CSV export is raw (unfiltered) on purpose: filtering is for display only, the
  analysis must see what the device produced.
