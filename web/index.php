<?php
/**
 * ECG dashboard — akvizicija (link na plotter) + rezultati + istorija snimanja.
 *   /ecg/            → poslednja analiza + istorija
 *   /ecg/?session=X  → analiza pojedinačnog snimka iz istorije
 */
declare(strict_types=1);
date_default_timezone_set('Europe/Belgrade');
header('Cache-Control: no-store');
define('ECG_DIR', __DIR__ . '/');
define('DATA_DIR', ECG_DIR . 'data/');
define('HIST_DIR', DATA_DIR . 'history/');

function read_json(?string $p): ?array {
    if (!$p || !is_file($p)) return null;
    $d = json_decode((string)file_get_contents($p), true);
    return is_array($d) ? $d : null;
}
function h($s): string { return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8'); }
function ms(?float $v): string { return $v === null ? '–' : number_format($v * 1000, 0); }
function num($v, int $dec = 0): string {
    return $v === null || $v === '' ? '–' : number_format((float)$v, $dec, ',', '.');
}

$session = isset($_GET['session']) ? (string)$_GET['session'] : null;
if ($session !== null && !preg_match('/^\d{8}-\d{6}$/', $session)) $session = null;

$viewDir = $session ? HIST_DIR . $session . '/' : DATA_DIR;
$result  = read_json($viewDir . 'data.json');
$report  = is_file($viewDir . 'data.txt') ? (string)file_get_contents($viewDir . 'data.txt') : '';
$job     = read_json(DATA_DIR . 'job.json');
$bust    = '?v=' . (@filemtime($viewDir . 'data.json') ?: time());

/* istorija (najnovije prvo) */
$history = [];
if (is_dir(HIST_DIR)) {
    foreach (scandir(HIST_DIR, SCANDIR_SORT_DESCENDING) as $d) {
        if (!preg_match('/^\d{8}-\d{6}$/', $d)) continue;
        $r = read_json(HIST_DIR . $d . '/data.json');
        $mbh = $r['median_beat'] ?? [];
        $pr_h = (is_array($mbh['p'] ?? null) && is_finite((float)($mbh['p']['pr_ms'] ?? NAN)))
            ? (float)$mbh['p']['pr_ms']
            : (isset($r['intervals']['pr']['median']) ? $r['intervals']['pr']['median'] * 1000 : null);
        $qrs_h = !empty($mbh['qrs_ms']) ? (float)$mbh['qrs_ms']
            : (isset($r['intervals']['qrs']['median']) ? $r['intervals']['qrs']['median'] * 1000 : null);
        $qtc_h = !empty($mbh['t']['qt_ms']) && !empty($r['heart']['median'])
            ? ((float)$mbh['t']['qt_ms']) / sqrt(60.0 / (float)$r['heart']['median'])
            : (isset($r['intervals']['qtc_bazett']['median']) ? $r['intervals']['qtc_bazett']['median'] * 1000 : null);
        $history[] = [
            'id' => $d,
            'when' => $r['generated'] ?? date('c', (int)filemtime(HIST_DIR . $d)),
            'hr' => $r['heart']['median'] ?? null,
            'beats' => $r['heart']['beats'] ?? null,
            'dur' => $r['sampling']['duration_s'] ?? null,
            'fs' => $r['sampling']['measured_hz'] ?? null,
            'q' => $r['quality']['score'] ?? null,
            'ql' => $r['quality']['label'] ?? null,
            'src' => $r['source'] ?? null,
            'pr' => $pr_h,
            'qrs' => $qrs_h,
            'qtc' => $qtc_h,
        ];
        if (count($history) >= 30) break;
    }
}

$h  = $result['heart'] ?? [];
$iv = $result['intervals'] ?? [];
$q  = $result['quality'] ?? [];
$s  = $result['sampling'] ?? [];
$ht = $result['hrv']['time'] ?? [];
$hf = $result['hrv']['freq'] ?? [];
?>
<!DOCTYPE html>
<html lang="sr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ECG — snimanje, analiza i istorija</title>
<style>
  :root { --bg:#f1f3fa; --card:#fff; --ink:#1b1f2a; --muted:#6b7280; --line:#e4e7f2;
          --accent:#5b5bd6; --ok:#16a34a; --err:#dc2626; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
  header { background:linear-gradient(135deg,#5b5bd6 0%,#7c3aed 100%); color:#fff; padding:18px 22px; }
  header h1 { margin:0 0 4px; font-size:1.35rem; font-weight:600; }
  header .sub { font-size:.88rem; opacity:.92; }
  header .sub a { color:#fff; }
  main { max-width:1280px; margin:0 auto; padding:18px; display:flex; flex-direction:column; gap:16px; }
  .card { background:var(--card); border-radius:14px; box-shadow:0 4px 16px rgba(20,25,60,.08); padding:16px 18px; }
  .card h2 { margin:0 0 12px; font-size:1.02rem; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }
  .kpi { background:#f7f8fd; border-radius:10px; padding:12px; }
  .kpi .l { font-size:.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .kpi .v { font-size:1.3rem; font-weight:650; font-variant-numeric:tabular-nums; }
  .kpi .u { font-size:.78rem; color:var(--muted); }
  table { width:100%; border-collapse:collapse; font-size:.86rem; }
  th, td { text-align:right; padding:7px 8px; border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--muted); font-weight:600; font-size:.76rem; text-transform:uppercase; letter-spacing:.03em; }
  tr:hover td { background:#f7f8fd; }
  a { color:var(--accent); }
  button { border:0; border-radius:9px; padding:10px 14px; font-size:.9rem; font-weight:600; cursor:pointer;
           background:#eceffb; color:#2b3160; }
  button.primary { background:linear-gradient(135deg,#5b5bd6,#7c3aed); color:#fff; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  input[type=number], input[type=file] { padding:8px 10px; border:1px solid var(--line); border-radius:8px; font-size:.9rem; }
  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  pre.report { background:#0f1422; color:#d8e0f5; padding:14px; border-radius:10px; max-height:520px;
               overflow:auto; font-size:.8rem; line-height:1.45; }
  .imgs figure { margin:0 0 14px; }
  .imgs img { width:100%; border-radius:10px; border:1px solid var(--line); }
  .imgs figcaption { font-size:.8rem; color:var(--muted); margin-top:6px; }
  .badge { display:inline-block; padding:3px 9px; border-radius:999px; font-size:.75rem; font-weight:600; background:#eef1f8; color:#4b5563; }
  .badge.good { background:#dcfce7; color:#166534; }
  .badge.mid { background:#fef3c7; color:#92400e; }
  .badge.bad { background:#fee2e2; color:#991b1b; }
  .msg { margin-top:10px; padding:10px 12px; border-radius:9px; font-size:.87rem; display:none; }
  .msg.show { display:block; }
  .msg.ok { background:#dcfce7; color:#14532d; }
  .msg.err { background:#fee2e2; color:#7f1d1d; }
  .msg.info { background:#eef2ff; color:#2b3160; }
  .hint { font-size:.8rem; color:var(--muted); line-height:1.5; margin-top:8px; }
  .bar { height:6px; background:#e5e9f5; border-radius:99px; overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; width:0; background:linear-gradient(90deg,#5b5bd6,#7c3aed); transition:width .3s; }
  ul { margin:6px 0 0 18px; }
  li { margin-bottom:2px; }
</style>
</head>
<body>
<header>
  <h1>ECG — snimanje, analiza i istorija</h1>
  <div class="sub">
    Snimi signal preko Web Serial plottera (u browseru) ili pošalji CSV — analiza se
    pokreće na serveru. &nbsp;·&nbsp; <a href="plotter.html">→ Otvori snimanje (Web Serial)</a>
  </div>
</header>

<main>
  <div class="card">
    <h2>Snimanje i nova analiza</h2>
    <div class="row">
      <a href="plotter.html"><button class="primary">Snimaj preko Web Serial (live prikaz)</button></a>
      <span class="hint" style="margin:0">snimi → <b>Analiziraj</b> → rezultat se pojavi ovde i u istoriji</span>
    </div>
    <div class="row" style="margin-top:14px">
      <input type="file" id="file" accept=".csv,.txt">
      <label class="hint" style="margin:0">nominalna fs (Hz)</label>
      <input type="number" id="fs" value="192" min="20" max="20000" style="width:110px">
      <button id="upload">Analiziraj CSV fajl</button>
    </div>
    <div class="msg" id="upMsg"></div>
    <div class="bar" id="upBarWrap" style="display:none"><i id="upBar"></i></div>
    <div class="hint">
      Prihvata se CSV sa kolonom <code>Timestamp,S1</code> (kao izvoz plottera) ili
      bilo koji fajl sa jednom numeričkom kolonom. Trajanje analize: 5–25 s.
    </div>
  </div>

<?php if ($session): ?>
  <div class="card">
    <h2>Snimak iz istorije: <?= h($session) ?> <a href="index.php" style="font-size:.8rem;margin-left:8px">← nazad na poslednju analizu</a></h2>
  </div>
<?php endif; ?>

<?php if (!$result): ?>
  <div class="card">
    <h2>Još nema rezultata</h2>
    <p class="hint">Pokreni snimanje preko plottera ili pošalji CSV fajl iznad — izveštaj,
       grafici i tumačenje se pojavljuju ovde.</p>
  </div>
<?php else: ?>
  <div class="card">
    <h2>Rezultat analize
      <span class="badge <?= ($q['score'] ?? 0) >= 70 ? 'good' : (($q['score'] ?? 0) >= 50 ? 'mid' : 'bad') ?>">
        kvalitet <?= h($q['score'] ?? '–') ?>/100 · <?= h($q['label'] ?? '') ?></span>
      <span class="hint" style="margin-left:8px"><?= h($result['generated'] ?? '') ?> · izvor: <?= h($result['source'] ?? '–') ?>
        <?= isset($result['session']) ? '· sesija ' . h($result['session']) : '' ?></span>
    </h2>

    <?php
      // intervali: primarno merenje sa medijalnog beata (pouzdanije od beat-po-beat)
      $mbi = $result['median_beat'] ?? [];
      $cons = $result['consensus'] ?? [];
      $cpr = $cons['pr'] ?? []; $cqrs = $cons['qrs'] ?? []; $cqt = $cons['qt'] ?? [];
      $pr_show = (is_array($mbi['p'] ?? null) && is_finite((float)($mbi['p']['pr_ms'] ?? NAN))) ? $mbi['p']['pr_ms'] / 1000 : ($iv['pr']['median'] ?? null);
      $qrs_show = !empty($mbi['qrs_ms']) ? $mbi['qrs_ms'] / 1000 : ($iv['qrs']['median'] ?? null);
      $qt_show = !empty($mbi['t']['qt_ms']) ? $mbi['t']['qt_ms'] / 1000 : ($iv['qt']['median'] ?? null);
      $rr_med_s = !empty($h['median']) ? 60.0 / (float)$h['median'] : null;
      $qtc_show = (!empty($mbi['t']['qt_ms']) && $rr_med_s) ? ($mbi['t']['qt_ms'] / 1000) / sqrt($rr_med_s) : ($iv['qtc_bazett']['median'] ?? null);
      $mbi_note = !empty($mbi['qrs_ms']) ? 'medijalni beat' : 'beat-po-beat';
      $qrs_note = $mbi_note; $qt_note = $mbi_note; $pr_note = $mbi_note;
      if (!empty($cqrs['ok']) && isset($cqrs['median'])) { $qrs_show = $cqrs['median'] / 1000; $qrs_note = 'konsenzus ' . (int)$cqrs['n_ok'] . '/' . (int)$cqrs['n_total']; }
      if (!empty($cqt['ok']) && isset($cqt['median'])) {
        $qt_show = $cqt['median'] / 1000;
        $qt_note = 'konsenzus ' . (int)$cqt['n_ok'] . '/' . (int)$cqt['n_total'];
        $qtc_show = $rr_med_s ? ($qt_show / sqrt($rr_med_s)) : $qtc_show;
      }
      if (!empty($cpr['ok']) && isset($cpr['median'])) {
        $pr_show = $cpr['median'] / 1000;
        $pr_note = 'konsenzus ' . (int)$cpr['n_ok'] . '/' . (int)$cpr['n_total'] . ' grupa';
      } elseif (!empty($cpr['n_ok'])) {
        $pr_note = 'ne tumači se: grupe se ne slažu (' . (int)round($cpr['min_ms']) . '–' . (int)round($cpr['max_ms']) . ' ms)';
      }
    ?>
    <div class="kpis">
      <div class="kpi"><div class="l">HR (medijana)</div><div class="v"><?= num($h['median'] ?? null) ?></div><div class="u">bpm · srednja <?= num($h['mean'] ?? null) ?></div></div>
      <div class="kpi"><div class="l">PR</div><div class="v"><?= ms($pr_show) ?></div><div class="u">ms · normalno 120–200 · <?= h($pr_note) ?></div></div>
      <div class="kpi"><div class="l">QRS</div><div class="v"><?= ms($qrs_show) ?></div><div class="u">ms · jednoodvodno, za trend · <?= h($qrs_note) ?></div></div>
      <div class="kpi"><div class="l">QT</div><div class="v"><?= ms($qt_show) ?></div><div class="u">ms · QTc B <?= ms($qtc_show) ?> (ne tumači se) · <?= h($qt_note) ?></div></div>
      <div class="kpi"><div class="l">QTc (Fridericia)</div><div class="v"><?= ms($qt_show && $rr_med_s ? $qt_show / pow($rr_med_s, 1 / 3) : ($iv['qtc_fridericia']['median'] ?? null)) ?></div><div class="u">ms · ne tumači se (nije validirano)</div></div>
      <div class="kpi"><div class="l">SDNN</div><div class="v"><?= num($ht['sdnn_ms'] ?? null) ?></div><div class="u">ms · ukupna HRV</div></div>
      <div class="kpi"><div class="l">RMSSD</div><div class="v"><?= num($ht['rmssd_ms'] ?? null) ?></div><div class="u">ms · vagalni tonus</div></div>
      <div class="kpi"><div class="l">LF/HF</div><div class="v"><?= num($hf['lf_hf'] ?? null, 2) ?></div><div class="u">odnos LF/HF snaga · orijentaciono</div></div>
      <div class="kpi"><div class="l">Beatova</div><div class="v"><?= num($h['beats'] ?? null) ?></div><div class="u">odbačeno RR <?= num($h['rr_artifacts'] ?? null) ?></div></div>
      <div class="kpi"><div class="l">Trajanje</div><div class="v"><?= num($s['duration_s'] ?? null, 1) ?> s</div><div class="u"><?= num($s['samples'] ?? null) ?> uzoraka</div></div>
    </div>

    <div class="hint" style="margin-top:12px">
      fs nominalna <b><?= $s['nominal_hz'] ? num($s['nominal_hz']) . ' Hz' : 'nije zadato' ?></b> ·
      izmerena <b><?= $s['measured_hz'] ? num($s['measured_hz'], 1) . ' Hz' : '–' ?></b>
      <?php if (!empty($s['mismatch_pct']) && $s['mismatch_pct'] > 5): ?>
        <b style="color:#92400e">(odstupanje <?= num($s['mismatch_pct'], 1) ?> %)</b>
      <?php endif; ?>
      · vremenska osa: <b><?= h($s['time_axis'] ?? '–') ?></b> · metod delineacije
      <b><?= h($iv['delineation_method'] ?? '–') ?></b>
    </div>

    <?php if (!empty($cons) && (!empty($cons['pr']) || !empty($cons['qrs']))): ?>
      <div style="background:#f5f3ff;border-left:4px solid #7c3aed;padding:12px 14px;border-radius:8px;margin-top:14px">
        <b>Konsenzus merenja</b>
        <span class="hint">— isti medijalni beat izmeren više puta: <?= (int)($cons['k'] ?? 0) ?> particije udara<?php
          if (!empty($cons['n_segments'])): ?> + <?= (int)$cons['n_segments'] ?> segmenata od <?= (int)($cons['seg_s'] ?? 0) ?> s<?php endif; ?>.
          Parametar se tumači samo ako se grupe slažu.</span>
        <table style="margin-top:8px;border-collapse:collapse;font-size:.9rem">
          <tr><th style="text-align:left;padding:2px 10px 2px 0">parametar</th>
              <th style="text-align:right;padding:2px 10px">medijana</th>
              <th style="text-align:right;padding:2px 10px">raspon</th>
              <th style="text-align:right;padding:2px 10px">IQR</th>
              <th style="text-align:left;padding:2px 0 2px 10px">grupe / odluka</th></tr>
          <?php foreach (['pr' => 'PR', 'qrs' => 'QRS', 'qt' => 'QT'] as $ck => $cn):
                 $c = $cons[$ck] ?? []; if (!$c) continue; ?>
            <tr>
              <td style="padding:2px 10px 2px 0"><b><?= $cn ?></b></td>
              <td style="text-align:right;padding:2px 10px"><?= isset($c['median']) ? (int)round($c['median']) . ' ms' : '–' ?></td>
              <td style="text-align:right;padding:2px 10px"><?= isset($c['min_ms']) ? (int)round($c['min_ms']) . '–' . (int)round($c['max_ms']) . ' ms' : '–' ?></td>
              <td style="text-align:right;padding:2px 10px"><?= isset($c['iqr_ms']) ? (int)round($c['iqr_ms']) : '–' ?></td>
              <td style="padding:2px 0 2px 10px">
                <?= (int)($c['n_ok'] ?? 0) ?>/<?= (int)($c['n_total'] ?? 0) ?> ·
                <?php if (!empty($c['ok'])): ?>
                  <b style="color:#15803d">slaže se — pouzdano</b>
                <?php else: ?>
                  <b style="color:#b91c1c">ne slaže se — ne tumači se</b>
                  <span class="hint"><?= h($c['reason'] ?? '') ?></span>
                <?php endif; ?>
              </td>
            </tr>
          <?php endforeach; ?>
        </table>
        <?php if (!empty($cons['state_change'])): ?>
          <div class="hint" style="margin-top:6px;color:#92400e"><b>⚠ Promena stanja u snimku:</b>
            puls po segmentima se menja <?= h($cons['hr_range_pct']) ?> % — merenje sa celog snimka
            meša dve morfologije; za takav snimak koristi razlaganje po fazama.</div>
        <?php endif; ?>
        <?php $srows = $cons['segment_rows'] ?? []; if ($srows): ?>
          <div class="hint" style="margin-top:8px">
            Po segmentima:
            <?php foreach ($srows as $r): ?>
              <span style="display:inline-block;margin:0 8px 4px 0"><?= (int)round($r['t0'] ?? 0) ?>–<?= (int)round($r['t1'] ?? 0) ?> s:
                HR <?= isset($r['hr']) ? (int)round($r['hr']) : '–' ?>,
                PR <?= isset($r['pr_ms']) ? (int)round($r['pr_ms']) : '–' ?>,
                QRS <?= isset($r['qrs_ms']) ? (int)round($r['qrs_ms']) : '–' ?>,
                QT <?= isset($r['qt_ms']) ? (int)round($r['qt_ms']) : '–' ?> ms</span>
            <?php endforeach; ?>
          </div>
        <?php endif; ?>
      </div>
    <?php endif; ?>

    <?php if (!empty($result['layman'])): ?>
      <div style="background:#ecfdf5;border-left:4px solid #16a34a;padding:12px 14px;border-radius:8px;margin-top:16px">
        <b>Objašnjenje bez stručnih izraza</b>
        <ul style="margin:6px 0 0 18px">
          <?php foreach ($result['layman'] as $line): ?>
            <li><?= h($line) ?></li>
          <?php endforeach; ?>
        </ul>
      </div>
    <?php endif; ?>

    <?php $med = $result['medical'] ?? []; $ex = $result['extra'] ?? []; $mbi = $result['median_beat'] ?? []; ?>
    <?php if (!empty($med['lines'])): ?>
      <div style="background:#eff6ff;border-left:4px solid #2563eb;padding:12px 14px;border-radius:8px;margin-top:14px">
        <b>Medicinsko tumačenje</b>
        <ul style="margin:6px 0 0 18px">
          <?php foreach ($med['lines'] as $line): ?><li><?= h($line) ?></li><?php endforeach; ?>
        </ul>
        <?php if (!empty($med['anomalies'])): ?>
          <b style="display:block;margin-top:8px">Odstupanja</b>
          <ul style="margin:4px 0 0 18px">
            <?php foreach ($med['anomalies'] as $a): ?><li style="color:#b91c1c"><?= h($a) ?></li><?php endforeach; ?>
          </ul>
        <?php else: ?>
          <div style="margin-top:6px;color:#166534">Nema izmerenih odstupanja od normalnih vrednosti.</div>
        <?php endif; ?>
        <?php if (!empty($med['checks'])): ?>
          <details style="margin-top:6px"><summary style="cursor:pointer">Uslovi pouzdanosti merenja</summary>
            <ul style="margin:4px 0 0 18px"><?php foreach ($med['checks'] as $c): ?><li><?= h($c) ?></li><?php endforeach; ?></ul>
          </details>
        <?php endif; ?>
        <?php if (!empty($med['cannot'])): ?>
          <details style="margin-top:6px"><summary style="cursor:pointer">Šta se ovim snimkom ne može utvrditi</summary>
            <ul style="margin:4px 0 0 18px"><?php foreach ($med['cannot'] as $c): ?><li><?= h($c) ?></li><?php endforeach; ?></ul>
          </details>
        <?php endif; ?>
        <?php if (!empty($med['next_steps'])): ?>
          <b style="display:block;margin-top:8px">Preporuke</b>
          <ul style="margin:4px 0 0 18px"><?php foreach ($med['next_steps'] as $s): ?><li><?= h($s) ?></li><?php endforeach; ?></ul>
        <?php endif; ?>
      </div>
    <?php endif; ?>

    <?php if ($ex || !empty($mbi['qrs_ms'])): ?>
      <div style="background:#f5f3ff;border-left:4px solid #7c3aed;padding:12px 14px;border-radius:8px;margin-top:14px">
        <b>Dodatne analize iz snimka</b>
        <table style="width:100%;font-size:.88rem;margin-top:6px">
          <?php if (!empty($mbi['qrs_ms'])): ?>
            <tr><td style="color:#6b7280;padding:2px 8px 2px 0">Medijalni beat (<?= (int)($mbi['n_beats'] ?? 0) ?> udara, sličnost <?= h($mbi['template_corr'] ?? '?') ?>)</td>
              <td>QRS <?= (int)round($mbi['qrs_ms']) ?> ms<?= is_finite((float)($mbi['p']['pr_ms'] ?? NAN)) ? ', PR ' . (int)round($mbi['p']['pr_ms']) . ' ms' : '' ?><?= !empty($mbi['t']['qt_ms']) ? ', QT ' . (int)round($mbi['t']['qt_ms']) . ' ms' : '' ?></td></tr>
          <?php endif; ?>
          <?php if (!empty($ex['ectopics'])): ?>
            <tr><td style="color:#6b7280;padding:2px 8px 2px 0">Ritam (RR medijana <?= (int)round($ex['ectopics']['median_rr_ms']) ?> ms)</td>
              <td>preuranjenih udara: <?= (int)$ex['ectopics']['premature'] ?>, pauza: <?= (int)$ex['ectopics']['pauses'] ?></td></tr>
          <?php endif; ?>
          <?php if (!empty($ex['respiration'])): ?>
            <tr><td style="color:#6b7280;padding:2px 8px 2px 0">Disanje</td>
              <td><?= h($ex['respiration']['breaths_per_min']) ?>/min (dominacija <?= h($ex['respiration']['dominance_pct']) ?>%)</td></tr>
          <?php endif; ?>
          <?php if (!empty($ex['nonlinear'])): ?>
            <tr><td style="color:#6b7280;padding:2px 8px 2px 0">Nelinearna HRV</td>
              <td>DFA α1 <?= h($ex['nonlinear']['dfa_alpha1']) ?>, SampEn <?= h($ex['nonlinear']['sample_entropy']) ?><?= !empty($ex['nonlinear']['orientational']) ? ' (orijentaciono)' : '' ?></td></tr>
          <?php endif; ?>
          <?php if (!empty($ex['st_trend']['range_pct_r'])): ?>
            <tr><td style="color:#6b7280;padding:2px 8px 2px 0">ST trend (relativno)</td>
              <td><?= h($ex['st_trend']['range_pct_r'][0]) ?>…<?= h($ex['st_trend']['range_pct_r'][1]) ?> %R, <?= !empty($ex['st_trend']['trend_ok']) ? h($ex['st_trend']['slope_pct_per_min'] ?? 0) . ' %R/min' : 'trend se ne ocenjuje (kratak snimak)' ?></td></tr>
          <?php endif; ?>
          <?php if (!empty($ex['noise_timeline'])):
            $worst = $ex['noise_timeline'][0];
            foreach ($ex['noise_timeline'] as $s) { if ($s['hf_pct'] > $worst['hf_pct']) $worst = $s; } ?>
            <tr><td style="color:#6b7280;padding:2px 8px 2px 0">Najgori deo snimka</td>
              <td><?= (int)round($worst['t0']) ?>–<?= (int)round($worst['t1']) ?> s (HF šum <?= h($worst['hf_pct']) ?>%)</td></tr>
          <?php endif; ?>
        </table>
      </div>
    <?php endif; ?>

    <h3 style="margin:16px 0 0; font-size:.95rem">Tumačenje</h3>
    <ul>
      <?php foreach (($result['interpretations'] ?? []) as $line): ?>
        <?php if (str_starts_with($line, '  ')): ?>
          <li style="list-style:none;margin-left:-14px"><i><?= h(trim($line)) ?></i></li>
        <?php else: ?>
          <li><?= h($line) ?></li>
        <?php endif; ?>
      <?php endforeach; ?>
    </ul>
    <?php if (!empty($q['notes'])): ?>
      <h3 style="margin:14px 0 0; font-size:.95rem">Napomene o kvalitetu signala</h3>
      <ul><?php foreach ($q['notes'] as $n): ?><li><?= h($n) ?></li><?php endforeach; ?></ul>
    <?php endif; ?>
    <p class="hint"><?= h($result['disclaimer'] ?? '') ?></p>

    <div class="imgs" style="margin-top:16px">
      <figure>
        <img src="<?= $session ? 'data/history/' . h($session) . '/data_report.png' . h($bust) : 'data/data_report.png' . h($bust) ?>" alt="izveštaj">
        <figcaption>Pregled: sirov signal, filtriran signal sa R vrhovima, RR tachogram, raspodela RR, Poincaré.</figcaption>
      </figure>
      <figure>
        <img src="<?= $session ? 'data/history/' . h($session) . '/data_intervals.png' . h($bust) : 'data/data_intervals.png' . h($bust) ?>" alt="intervali">
        <figcaption>Delineacija P/QRS/T i izmereni intervali (prvih 10 s) + medijalni beat.</figcaption>
      </figure>
    </div>

    <details style="margin-top:10px">
      <summary style="cursor:pointer;font-size:.85rem;color:var(--muted)">Pun tekstualni izveštaj</summary>
      <pre class="report" style="margin-top:10px"><?= h($report) ?></pre>
    </details>

    <div class="row" style="margin-top:12px; font-size:.85rem">
      <span>Preuzmi:</span>
      <a href="<?= $session ? 'data/history/' . h($session) . '/data.csv' : 'data/data.csv' ?>">CSV snimak</a>
      <a href="<?= $session ? 'data/history/' . h($session) . '/data.json' : 'data/data.json' ?>">JSON rezultati</a>
      <a href="<?= $session ? 'data/history/' . h($session) . '/data.txt' : 'data/data.txt' ?>">izveštaj (.txt)</a>
    </div>
  </div>
<?php endif; ?>

  <div class="card">
    <h2>Istorija snimanja <span class="hint">(poslednjih <?= count($history) ?>)</span></h2>
    <?php if (!$history): ?>
      <p class="hint">Istorija je prazna — svaka analiza se automatski arhivira (do 40 snimaka).</p>
    <?php else: ?>
      <table>
        <tr><th>Snimak</th><th>HR</th><th>PR</th><th>QRS</th><th>QTc</th><th>Kvalitet</th><th>Trajanje</th><th>Beatova</th><th>Odstupanje fs</th></tr>
        <?php foreach ($history as $it): ?>
          <tr>
            <td><a href="index.php?session=<?= h($it['id']) ?>"><?= h(str_replace('-', ' ', $it['id'])) ?></a></td>
            <td><?= num($it['hr']) ?></td>
            <td><?= num($it['pr']) ?></td>
            <td><?= num($it['qrs']) ?></td>
            <td><?= num($it['qtc']) ?></td>
            <td><span class="badge <?= ($it['q'] ?? 0) >= 70 ? 'good' : (($it['q'] ?? 0) >= 50 ? 'mid' : 'bad') ?>"><?= h($it['q'] ?? '–') ?> · <?= h($it['ql'] ?? '') ?></span></td>
            <td><?= num($it['dur'], 1) ?> s</td>
            <td><?= num($it['beats']) ?></td>
            <td><?= $it['fs'] ? num($it['fs'], 1) . ' Hz' : '–' ?></td>
          </tr>
        <?php endforeach; ?>
      </table>
      <div class="hint">Klikni na snimak za pun izveštaj i grafike. Izvor: serial / sim / csv / upload.
        Trend HR i QTc kroz istoriju je najkorisniji ako snimaš u sličnim uslovima (mirovanje, isto doba dana).</div>
    <?php endif; ?>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
function msg(text, cls) { const m = $("upMsg"); m.textContent = text; m.className = "msg show " + cls; }

let poll = null;
$("upload").onclick = async () => {
  const f = $("file").files[0];
  if (!f) { msg("Izaberi CSV fajl.", "err"); return; }
  const fd = new FormData();
  fd.append("data_file", f, f.name);
  fd.append("fs", $("fs").value);
  fd.append("source", "upload");
  msg("Šaljem fajl i pokrećem analizu…", "info");
  $("upBarWrap").style.display = "block";
  $("upBar").style.width = "12%";
  $("upload").disabled = true;
  try {
    const r = await fetch("api.php?action=submit", { method: "POST", body: fd });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || ("HTTP " + r.status));
    msg("Analiza je pokrenuta…", "info");
    let el = 0;
    clearInterval(poll);
    poll = setInterval(async () => {
      el += 2;
      $("upBar").style.width = Math.min(90, 12 + el * 4) + "%";
      try {
        const st = await (await fetch("api.php?action=status&t=" + Date.now())).json();
        if (st.state === "done") {
          clearInterval(poll); $("upBar").style.width = "100%";
          msg("Završeno — osvežavam stranicu…", "ok");
          setTimeout(() => location.href = "index.php", 800);
        } else if (st.state === "failed") {
          clearInterval(poll); $("upload").disabled = false;
          msg("Analiza nije uspela: " + (st.error || "nepoznata greška"), "err");
        } else {
          msg("Obrada u toku… (" + el + " s)", "info");
        }
      } catch (e) {}
    }, 2000);
  } catch (err) {
    $("upload").disabled = false;
    msg("Greška: " + err.message, "err");
  }
};
function hideBar() { $("upBarWrap").style.display = "none"; }
</script>
</body>
</html>
