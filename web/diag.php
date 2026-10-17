<?php
// Prijem dijagnostike serial porta sa telefona/desktopа (serial_diag.html).
// Upisuje liniju JSON-a u data/serial_diag.log. Ništa drugo ne dira.
header('Content-Type: text/plain; charset=utf-8');
date_default_timezone_set('Europe/Belgrade');

if ($_SERVER['REQUEST_METHOD'] !== 'POST') { http_response_code(405); echo "POST only\n"; exit; }

$raw = file_get_contents('php://input');
if ($raw === false || $raw === '') { http_response_code(400); echo "prazan body\n"; exit; }
if (strlen($raw) > 16384) { $raw = substr($raw, 0, 16384); }   // ograničenje veličine

$j = json_decode($raw, true);
if (!is_array($j)) { http_response_code(400); echo "nije JSON\n"; exit; }

$dir = __DIR__ . '/data';
if (!is_dir($dir)) { http_response_code(500); echo "nema data/\n"; exit; }

$line = json_encode([
    'received' => date('c'),
    'ip'       => $_SERVER['REMOTE_ADDR'] ?? '',
    'data'     => $j,
], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n";

$f = $dir . '/serial_diag.log';
$ok = @file_put_contents($f, $line, FILE_APPEND | LOCK_EX);
echo $ok === false ? "upis nije uspeo\n" : "ok (" . $ok . " bajtova)\n";
