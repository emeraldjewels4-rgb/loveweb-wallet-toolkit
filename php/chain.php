<?php
declare(strict_types=1);

/**
 * The blockchain interface: `lovewebconnection` wired to Polygon PoS.
 *
 *   GET chain.php                 HTML
 *   GET chain.php?format=json     the same facts as JSON
 *
 * Where the settings come from: one row in `chain_connections`, in the primary
 * database, keyed by the label `lovewebconnection`. Stored rather than hardcoded
 * so changing the endpoint is an UPDATE instead of a deploy -- and so `on` is a
 * fact somebody set, not something a page infers from having rendered. The row is
 * seeded switched on (enabled = 1, on = 1); switching it off later is an UPDATE,
 * which is a feature the interface reports rather than one it quietly assumes.
 *
 * Why the row is not in `lovewebconnection` itself: that database is SELECT-only
 * for this user. `db_sync.php?action=report` establishes that from SHOW GRANTS
 * instead of guessing, and this page repeats the finding in `storage` rather than
 * pretending the write went where the label points.
 *
 * Every network call below is a read -- eth_chainId, eth_blockNumber,
 * eth_gasPrice. There is no send path in this file and no key on the host, so
 * "connected" here can never quietly mean "able to move funds".
 *
 * The `web9999` gateway is the reason this page exists in the shape it does: the
 * account holds 28 `ww9999.*` hosts and no domain literally called `web9999`, so
 * the group is matched as `w*9999.` and reports one `all_on` bit for all of them.
 */

require_once __DIR__ . '/broadcast.php';
require_once __DIR__ . '/db.php';

$settings = chain_status();

$live = null;
$error = '';
try {
    $rpc = new JsonRpc((string) $settings['rpc'], 20);
    $live = array(
        'chain_id' => (int) hexdec((string) $rpc->call('eth_chainId')),
        'block' => (int) hexdec((string) $rpc->call('eth_blockNumber')),
        'gas_price_gwei' => Wei::scale(Wei::fromHex((string) $rpc->call('eth_gasPrice')), 9, 3),
        'checked_at' => gmdate('c'),
    );
    // The RPC saying 137 is the check that "Polygon" is not just a label in a
    // config file: a wrong endpoint answers here, before anything relies on it.
    $live['chain_id_matches'] = $live['chain_id'] === (int) $settings['chain_id'];
} catch (RpcError $exc) {
    $error = 'rpc: ' . $exc->getMessage();
}

$payload = $settings;
$payload['signs'] = false;
$payload['live'] = $live;
$payload['error'] = $error === '' ? null : $error;

$format = isset($_GET['format']) ? strtolower((string) $_GET['format']) : '';
if ($format === 'json') {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES, 'UTF-8');
}

function bit(int $value): string
{
    return $value === 1 ? '01' : '00';
}

$gateway = $settings['gateway'];
$bound = $settings['bound'];
$domains = $settings['domains'];
$network = $settings['network_hosts'];
$shown = 0;
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Blockchain interface — <?php echo h((string) $settings['label']); ?> on <?php echo h((string) $settings['network']); ?></title>
<meta name="robots" content="noindex" />
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 2rem 1.25rem 4rem; background: #020617; color: #e2e8f0;
         font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }
  main { max-width: 46rem; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; color: #f8fafc; }
  h2 { font-size: 1rem; margin: 2.25rem 0 .5rem; color: #f8fafc; }
  p.lead { color: #94a3b8; margin: 0 0 1.5rem; }
  table { width: 100%; border-collapse: collapse; margin: .5rem 0 1.5rem; }
  th, td { text-align: left; padding: .45rem .5rem; border-bottom: 1px solid #1e293b; font-size: .9rem; }
  th { color: #94a3b8; font-weight: 500; width: 11rem; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; color: #fdba74; }
  a { color: #fdba74; }
  .err { border-left: 3px solid #ef4444; padding: .6rem .8rem; background: #1f1315; color: #fca5a5; border-radius: .25rem; }
  .warn { border-left: 3px solid #f97316; padding: .6rem .8rem; background: #1b1207; color: #fdba74; border-radius: .25rem; }
  footer { margin-top: 2.5rem; color: #64748b; font-size: .8rem; }
</style>
</head>
<body>
<main>
  <h1>Blockchain interface</h1>
  <p class="lead">
    <code><?php echo h((string) $settings['label']); ?></code> on
    <?php echo h(ucfirst((string) $settings['network'])); ?>.
    Read-only: this page holds no key and cannot sign or broadcast.
  </p>

  <?php if ($error !== ''): ?>
    <p class="err"><?php echo h($error); ?></p>
  <?php endif; ?>

  <table>
    <tr><th>Interface</th><td><code><?php echo bit((int) $settings['on']); ?></code>
      <?php echo (int) $settings['on'] === 1 ? 'live' : 'off'; ?>
      <?php if ($settings['updated_at'] !== null): ?>
        <span style="color:#64748b">&middot; set <?php echo h((string) $settings['updated_at']); ?> UTC</span>
      <?php endif; ?></td></tr>
    <tr><th>Network</th><td><?php echo h((string) $settings['network']); ?> (chain id
      <code><?php echo (int) $settings['chain_id']; ?></code>)</td></tr>
    <tr><th>RPC endpoint</th><td><code><?php echo h((string) $settings['rpc']); ?></code></td></tr>
    <tr><th>Explorer</th><td><a href="<?php echo h((string) $settings['explorer']); ?>" rel="noreferrer noopener">
      <?php echo h((string) $settings['explorer']); ?></a></td></tr>
    <tr><th>Block</th><td><?php echo $live === null ? '&mdash;' : h(Wei::thousands((string) $live['block'])); ?></td></tr>
    <tr><th>Gas price</th><td><?php echo $live === null ? '&mdash;' : h((string) $live['gas_price_gwei']) . ' gwei'; ?></td></tr>
    <tr><th>RPC chain id</th><td>
      <?php if ($live === null): ?>&mdash;
      <?php else: ?>
        <code><?php echo (int) $live['chain_id']; ?></code>
        <?php echo $live['chain_id_matches'] ? 'matches the stored connection' : '<span class="warn">does not match</span>'; ?>
      <?php endif; ?>
    </td></tr>
    <tr><th>Settings</th><td>stored in
      <code><?php echo h((string) ($settings['storage']['database'] ?? '?')); ?>.<?php echo h((string) $settings['storage']['table']); ?></code>
      (source: <code><?php echo h((string) $settings['source']); ?></code>)</td></tr>
    <tr><th>Bound database</th><td>
      <code><?php echo h((string) $bound['label']); ?></code> &mdash;
      <code><?php echo bit((int) $bound['on']); ?></code>
      <?php echo $bound['connected']
          ? 'connected, ' . (int) $bound['tables'] . ' table(s)'
          : 'not reachable'; ?>
    </td></tr>
    <tr><th>Gateway</th><td>
      <code><?php echo h((string) $gateway['name']); ?></code> &mdash;
      <?php echo (int) $gateway['matches']; ?> host(s),
      <?php echo (int) $gateway['on']; ?> on,
      all together <code><?php echo bit((int) $gateway['all_on']); ?></code>
    </td></tr>
    <tr><th>All domains</th><td>
      <?php echo (int) $domains['total']; ?> bound, <?php echo (int) $domains['on']; ?> on,
      all together <code><?php echo bit((int) $domains['all_on']); ?></code>
    </td></tr>
    <tr><th><?php echo h((string) $network['suffix']); ?> domains</th><td>
      <?php echo (int) $network['total']; ?> bound, <?php echo (int) $network['on']; ?> on,
      all together <code><?php echo bit((int) $network['all_on']); ?></code>
    </td></tr>
  </table>

  <?php if ($bound['connected']): ?>
    <h2>What <?php echo h((string) $bound['label']); ?> actually grants</h2>
    <p class="lead" style="margin:.25rem 0 .75rem;">
      Read on this request: the tables the binding can see, and the privileges
      <code>SHOW GRANTS</code> returns for this user. A row count is
      <code>information_schema</code>'s estimate, not a scan of somebody else's data.
    </p>
    <table>
      <tr><th style="width:auto">Table</th><th style="width:auto">Rows (estimate)</th></tr>
      <?php foreach ($bound['table_list'] as $table): ?>
        <tr>
          <td><code><?php echo h((string) $table['name']); ?></code></td>
          <td><?php echo (int) $table['rows_estimate']; ?></td>
        </tr>
      <?php endforeach; ?>
      <?php if (!$bound['table_list']): ?>
        <tr><td colspan="2" style="color:#94a3b8">
          No tables are visible to this grant &mdash; connected, but nothing readable.
        </td></tr>
      <?php endif; ?>
    </table>
    <table>
      <tr><th style="width:auto">Privilege</th></tr>
      <?php foreach ($bound['grants'] as $grant): ?>
        <tr><td style="word-break:break-word"><code><?php echo h($grant); ?></code></td></tr>
      <?php endforeach; ?>
      <?php if (!$bound['grants']): ?>
        <tr><td style="color:#94a3b8"><code>SHOW GRANTS</code> returned nothing, so what this
          binding may do is unknown rather than unrestricted.</td></tr>
      <?php endif; ?>
    </table>
  <?php endif; ?>

  <?php if ($gateway['matches'] > 0): ?>
    <h2>The <?php echo h((string) $gateway['name']); ?> gateway</h2>
    <p class="lead" style="margin:.25rem 0 .75rem;">
      Matched as <code><?php echo h((string) $gateway['pattern']); ?></code> against every binding on
      the account: <?php echo h((string) $gateway['first']); ?> &rarr; <?php echo h((string) $gateway['last']); ?>.
    </p>
    <table>
      <tr><th style="width:auto">Host</th><th style="width:auto">On</th><th style="width:auto">Database</th></tr>
      <?php foreach ($gateway['domains'] as $member): ?>
        <?php if ($shown >= 12) { break; } $shown++; ?>
        <tr>
          <td><code><?php echo h((string) $member['domain']); ?></code></td>
          <td><code><?php echo bit((int) $member['on']); ?></code></td>
          <td><?php echo $member['database'] === null ? '&mdash;' : '<code>' . h((string) $member['database']) . '</code>'; ?></td>
        </tr>
      <?php endforeach; ?>
      <?php if ($gateway['matches'] > $shown): ?>
        <tr><td colspan="3" style="color:#94a3b8">&hellip; and <?php echo (int) ($gateway['matches'] - $shown); ?> more,
          all bound the same way.</td></tr>
      <?php endif; ?>
    </table>
  <?php endif; ?>

  <p class="lead" style="margin:1.5rem 0 0;">
    JSON for the same facts: <a href="?format=json">chain.php?format=json</a> &middot;
    <a href="preview.php?network=<?php echo h((string) $settings['network']); ?>">preview.php</a> &middot;
    <a href="index.php">toolkit</a>
  </p>

  <footer>
    Reads only: <code>eth_chainId</code>, <code>eth_blockNumber</code>, <code>eth_gasPrice</code>.
    Distribution still runs locally, where the key lives.
  </footer>
</main>
</body>
</html>
