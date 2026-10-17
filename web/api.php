<?php
/**
 * ECG API — prijem snimka, pokretanje analize u pozadini, status i istorija.
 *
 *   POST api.php?action=submit      multipart: data_file (CSV), fs, source
 *   GET  api.php?action=status      status posla (job.json)
 *   GET  api.php?action=latest      poslednji rezultat (JSON + tekst izveštaja)
 *   GET  api.php?action=history     lista snimljenih sesija
 *   GET  api.php?action=session&id=YYYYmmdd-HHMMSS
 */
declare(strict_types=1);
date_default_timezone_set('Europe/Belgrade');
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

define('ECG_DIR', __DIR__ . '/');
define('DATA_DIR', ECG_DIR . 'data/');
define('HIST_DIR', DATA_DIR . 'history/');
define('PYTHON', '/opt/ecg-venv/bin/python3');
define('ANALYZER', ECG_DIR . 'ecg_analyze.py');
define('LOCK_FILE', DATA_DIR . '.analysis.lock');
define('JOB_FILE', DATA_DIR . 'job.json');
define('RATE_FILE', DATA_DIR . '.ratelimit.json');
define('MAX_BYTES', 64 * 1024 * 1024);
define('KEEP_SESSIONS', 40);

function out(array $data, int $code = 200): void {
    http_response_code($code);
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}
function fail(string $msg, int $code = 400): void { out(['ok' => false, 'error' => $msg], $code); }

function read_json(string $path): ?array {
    if (!is_file($path)) return null;
    $d = json_decode((string)file_get_contents($path), true);
    return is_array($d) ? $d : null;
}
function write_json(string $path, array $data): void {
    $tmp = $path . '.tmp' . getmypid();
    file_put_contents($tmp, json_encode($data, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    @chmod($tmp, 0644);
    rename($tmp, $path);
}
/** Putanja relativna na /ecg/ (za <img src> sa ove i dashboard stranice). */
function rel(string $abs): string { return str_replace(ECG_DIR, '', $abs); }

function files_for(array $results, string $dirAbs, string $prefix = 'data'): array {
    $files = [
        'report_png'    => rel($dirAbs . $prefix . '_report.png'),
        'intervals_png' => rel($dirAbs . $prefix . '_intervals.png'),
        'results_json'  => rel($dirAbs . $prefix . '.json'),
        'report_txt'    => rel($dirAbs . $prefix . '.txt'),
        'csv'           => rel($dirAbs . 'data.csv'),
    ];
    foreach ($files as $k => $v) {
        if (!is_file(ECG_DIR . $v)) unset($files[$k]);
    }
    return $files;
}

function payload(string $dirAbs, ?array $job): array {
    $results = read_json($dirAbs . 'data.json');
    $prefix = 'data';
    $report = is_file($dirAbs . 'data.txt') ? (string)file_get_contents($dirAbs . 'data.txt') : '';
    return [
        'ok' => true,
        'state' => $job['state'] ?? 'none',
        'job_id' => $job['id'] ?? null,
        'started' => $job['started'] ?? null,
        'finished' => $job['finished'] ?? null,
        'error' => $job['error'] ?? null,
        'results' => $results,
        'report_text' => $report,
        'files' => files_for($results ?? [], $dirAbs, $prefix),
    ];
}

$action = $_GET['action'] ?? 'latest';


/* ------------------------------------------------------------------ status -- */
if ($action === 'status') {
    $job = read_json(JOB_FILE);
    if (!$job) fail('nema posla', 404);
    if (($job['state'] ?? '') === 'running') {
        // zaštita: ako je proces umro bez upisa statusa
        $age = time() - (int)($job['started_ts'] ?? time());
        $proc = @file_get_contents(DATA_DIR . '.pid');
        if ($age > 900) {
            $job['state'] = 'failed';
            $job['error'] = 'Analiza je prekinuta (isteklo vreme).';
            write_json(JOB_FILE, $job);
        }
    }
    if (($job['state'] ?? '') === 'done') out(payload(DATA_DIR, $job));
    out(['ok' => true, 'state' => $job['state'] ?? 'running', 'job_id' => $job['id'] ?? null,
         'error' => $job['error'] ?? null]);
}

/* ------------------------------------------------------------------ latest -- */
if ($action === 'latest') {
    $job = read_json(JOB_FILE);
    if (!is_file(DATA_DIR . 'data.json') && !$job) fail('još nema nijedne analize', 404);
    out(payload(DATA_DIR, $job));
}

/* ----------------------------------------------------------------- history -- */
if ($action === 'history') {
    $items = [];
    if (is_dir(HIST_DIR)) {
        foreach (scandir(HIST_DIR, SCANDIR_SORT_DESCENDING) as $d) {
            if ($d === '.' || $d === '..' || !is_dir(HIST_DIR . $d)) continue;
            $r = read_json(HIST_DIR . $d . '/data.json');
            $items[] = [
                'id' => $d,
                'when' => $r['generated'] ?? date('c', (int)filemtime(HIST_DIR . $d)),
                'hr' => $r['heart']['median'] ?? null,
                'beats' => $r['heart']['beats'] ?? null,
                'duration_s' => $r['sampling']['duration_s'] ?? null,
                'samples' => $r['sampling']['samples'] ?? null,
                'fs_measured' => $r['sampling']['measured_hz'] ?? null,
                'quality' => $r['quality']['score'] ?? null,
                'quality_label' => $r['quality']['label'] ?? null,
                'source' => $r['source'] ?? null,
                // PR/QRS: merodavna je odluka motora — konsenzus, pa medicinska sekcija
                // (pr_used_ms = null znači „ne tumači se"), a beat-po-beat (dwt) samo ako
                // snimak nema nove sekcije (stare arhive).
                'pr_ms' => (isset($r['consensus']['pr']['ok']) && $r['consensus']['pr']['ok']
                            && isset($r['consensus']['pr']['median']))
                    ? $r['consensus']['pr']['median']
                    : (isset($r['medical'])
                        ? ($r['medical']['pr_used_ms'] ?? null)
                        : (isset($r['intervals']['pr']['median']) ? $r['intervals']['pr']['median'] * 1000 : null)),
                'qrs_ms' => (isset($r['consensus']['qrs']['ok']) && $r['consensus']['qrs']['ok']
                            && isset($r['consensus']['qrs']['median']))
                    ? $r['consensus']['qrs']['median']
                    : (isset($r['medical'])
                        ? ($r['medical']['qrs_used_ms'] ?? null)
                        : (isset($r['intervals']['qrs']['median']) ? $r['intervals']['qrs']['median'] * 1000 : null)),
                'qtc_ms' => (isset($r['medical'])
                        ? ($r['medical']['qtc_ms'] ?? null)
                        : (isset($r['intervals']['qtc_bazett']['median']) ? $r['intervals']['qtc_bazett']['median'] * 1000 : null)),
            ];
        }
    }
    out(['ok' => true, 'items' => $items]);
}

/* ----------------------------------------------------------------- session -- */
if ($action === 'session') {
    $id = (string)($_GET['id'] ?? '');
    if (!preg_match('/^\d{8}-\d{6}$/', $id)) fail('neispravan id sesije');
    $dir = HIST_DIR . $id . '/';
    if (!is_dir($dir)) fail('sesija ne postoji', 404);
    out(payload($dir, ['state' => 'done', 'id' => $id]));
}

/* ------------------------------------------------------------------ submit -- */
if ($action === 'submit') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail('samo POST', 405);
    if (empty($_FILES['data_file']) || $_FILES['data_file']['error'] !== UPLOAD_ERR_OK) {
        $code = $_FILES['data_file']['error'] ?? -1;
        fail($code === UPLOAD_ERR_INI_SIZE ? 'fajl je veći od dozvoljenog (php.ini)' : "greška pri uploadu (kod $code)");
    }
    $f = $_FILES['data_file'];
    if ((int)$f['size'] > MAX_BYTES) fail('fajl je prevelik (max ' . (MAX_BYTES / 1048576) . ' MB)');
    $ext = strtolower(pathinfo((string)$f['name'], PATHINFO_EXTENSION));
    if (!in_array($ext, ['csv', 'txt'], true)) fail('dozvoljeni su samo .csv i .txt');

    // rate limit: 30 analiza po satu po IP adresi
    $ip = $_SERVER['HTTP_CF_CONNECTING_IP'] ?? $_SERVER['REMOTE_ADDR'] ?? '?';
    $rl = read_json(RATE_FILE) ?: [];
    $now = time();
    $rl = array_filter($rl, fn($t) => $now - (int)$t < 3600);
    if (count(array_filter(array_keys($rl), fn($k) => str_starts_with($k, $ip))) >= 30) {
        fail('previše analiza u zadnjih sat vremena — sačekaj malo', 429);
    }
    $rl[$ip . '|' . $now] = $now;
    write_json(RATE_FILE, $rl);

    if (!is_dir(DATA_DIR)) @mkdir(DATA_DIR, 0775, true);
    if (!is_dir(HIST_DIR)) @mkdir(HIST_DIR, 0775, true);

    // samo jedan posao u datom trenutku
    $lock = @fopen(LOCK_FILE, 'c');
    if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) fail('analiza je već u toku — sačekaj da završi', 409);

    $tmp = DATA_DIR . '.upload.' . getmypid() . '.csv';
    if (!move_uploaded_file($f['tmp_name'], $tmp)) { flock($lock, LOCK_UN); fail('ne mogu da sačuvam fajl'); }
    @chmod($tmp, 0664);

    // osnovna validacija sadržaja (bar 100 uzoraka, numeričke vrednosti)
    $fh = fopen($tmp, 'r');
    $lines = 0; $numeric = 0;
    while (($line = fgets($fh)) !== false && $lines < 500) {
        $lines++;
        if (preg_match('/[-+]?\d/', $line)) $numeric++;
    }
    fclose($fh);
    if ($lines < 10 || $numeric < 5) {
        @unlink($tmp); flock($lock, LOCK_UN);
        fail('fajl ne izgleda kao snimak (nema numeričkih uzoraka)');
    }

    rename($tmp, DATA_DIR . 'data.csv');
    @chmod(DATA_DIR . 'data.csv', 0664);

    $fs = isset($_POST['fs']) ? (float)$_POST['fs'] : 0;
    if ($fs < 20 || $fs > 50000) $fs = 0;
    $source = preg_replace('/[^a-z]/', '', strtolower((string)($_POST['source'] ?? 'upload'))) ?: 'upload';

    $id = date('Ymd-His');
    write_json(JOB_FILE, ['id' => $id, 'state' => 'running', 'started' => date('c'),
                          'started_ts' => time(), 'source' => $source, 'fs' => $fs ?: null]);

    $args = [
        PYTHON, ANALYZER,
        '--input', DATA_DIR . 'data.csv',
        '--outdir', DATA_DIR,
        '--prefix', 'data',
        '--job-json', JOB_FILE,
        '--archive-dir', HIST_DIR,
        '--source', $source,
    ];
    if ($fs > 0) { $args[] = '--fs'; $args[] = (string)$fs; }
    $cmd = 'setsid nohup ' . implode(' ', array_map('escapeshellarg', $args))
         . ' >> ' . escapeshellarg(DATA_DIR . 'analysis.log') . ' 2>&1 < /dev/null & echo $!';
    $pid = trim((string)shell_exec($cmd));
    file_put_contents(DATA_DIR . '.pid', $pid);

    flock($lock, LOCK_UN);
    fclose($lock);

    out(['ok' => true, 'job_id' => $id, 'pid' => (int)$pid, 'fs' => $fs ?: null,
         'message' => 'analiza je pokrenuta']);
}

fail('nepoznata akcija: ' . $action, 404);
