<?php
declare(strict_types=1);

/**
 * Read-only status page: chain state, wallet balances, and the cost of a plan.
 * Server-rendered so it works with PHP alone -- no build step, no JS framework.
 *
 * Upload this directory as a SUBDIRECTORY (e.g. /toolkit/), not over the site
 * root: public_html already holds the FiatLord SPA, and an index.php in the root
 * can take over as DirectoryIndex.
 */

require_once __DIR__ . '/broadcast.php';
require_once __DIR__ . '/db.php';

$networks = array(
    'amoy' => array('name' => 'Polygon Amoy testnet', 'chain_id' => 80002,
                    'rpc' => 'https://polygon-amoy-bor-rpc.publicnode.com',
                    'currency' => 'POL', 'testnet' => true),
    'polygon' => array('name' => 'Polygon PoS mainnet', 'chain_id' => 137,
                       'rpc' => 'https://polygon-bor-rpc.publicnode.com',
                       'currency' => 'POL', 'testnet' => false),
);

$slug = isset($_GET['network']) && isset($networks[(string) $_GET['network']])
    ? (string) $_GET['network'] : 'amoy';
$net = $networks[$slug];

$addressInput = isset($_GET['addresses']) ? trim((string) $_GET['addresses']) : '';
$amount = isset($_GET['amount']) ? trim((string) $_GET['amount']) : '';

$addresses = array();
$inputError = '';
foreach (explode(',', $addressInput) as $candidate) {
    $candidate = trim($candidate);
    if ($candidate === '') {
        continue;
    }
    if (!preg_match('/^0x[0-9a-fA-F]{40}$/', $candidate)) {
        $inputError = 'Not an address: ' . htmlspecialchars(substr($candidate, 0, 60), ENT_QUOTES, 'UTF-8');
        $addresses = array();
        break;
    }
    $addresses[] = strtolower($candidate);
    if (count($addresses) >= 50) {
        break;
    }
}
if ($amount !== '' && !preg_match('/^[0-9]+(\.[0-9]+)?$/', $amount)) {
    $inputError = 'Amount must be a plain decimal number.';
}

$db = db_status();
$chain = null;
$rpcError = '';
$balances = array();
$plan = null;

if ($inputError === '') {
    try {
        $rpc = new JsonRpc($net['rpc'], 20);
        $gasPriceWei = Wei::fromHex((string) $rpc->call('eth_gasPrice'));
        $chain = array(
            'id' => (int) hexdec((string) $rpc->call('eth_chainId')),
            'block' => (int) hexdec((string) $rpc->call('eth_blockNumber')),
            'gas_gwei' => Wei::scale($gasPriceWei, 9, 3),
        );
        foreach ($addresses as $address) {
            // String math: a balance above ~9.2 POL overflows a PHP int and
            // hexdec() would quietly hand back an imprecise float.
            $balances[] = array(
                'address' => $address,
                'wei' => Wei::fromHex((string) $rpc->call('eth_getBalance', array($address, 'latest'))),
            );
        }
        if ($amount !== '' && $addresses !== array()) {
            $count = count($addresses);
            $units = Wei::parseAmount($amount);
            $valueWei = Wei::mulSmall($units, $count);
            $feeWei = Wei::mulSmall($gasPriceWei, 21000 * $count);
            $plan = array(
                'count' => $count,
                'amount' => $amount,
                'value_wei' => $valueWei,
                'fee_wei' => $feeWei,
                'total_wei' => Wei::add($valueWei, $feeWei),
            );
        }
    } catch (RpcError $exc) {
        $rpcError = $exc->getMessage();
    }
}

function pol(string $wei): string
{
    return Wei::scale($wei, 18, 6);
}

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES, 'UTF-8');
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Toolkit preview — <?php echo h($net['name']); ?></title>
<meta name="robots" content="noindex" />
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 2rem 1.25rem 4rem; background: #020617; color: #e2e8f0;
         font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }
  main { max-width: 46rem; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; color: #f8fafc; }
  p.lead { color: #94a3b8; margin: 0 0 1.75rem; }
  form { display: grid; gap: .75rem; margin-bottom: 2rem; }
  label { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: #94a3b8; }
  select, input { width: 100%; padding: .55rem .7rem; border-radius: .5rem; border: 1px solid #1e293b;
                  background: #0b1220; color: #e2e8f0; font: inherit; }
  button { justify-self: start; padding: .55rem 1.1rem; border: 0; border-radius: .5rem;
           background: #f97316; color: #0b1220; font-weight: 600; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin: .5rem 0 1.5rem; }
  th, td { text-align: left; padding: .45rem .5rem; border-bottom: 1px solid #1e293b; font-size: .9rem; }
  th { color: #94a3b8; font-weight: 500; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; color: #fdba74; }
  .err { border-left: 3px solid #ef4444; padding: .6rem .8rem; background: #1f1315; color: #fca5a5; border-radius: .25rem; }
  .warn { border-left: 3px solid #f97316; padding: .6rem .8rem; background: #1b1207; color: #fdba74; border-radius: .25rem; }
  footer { margin-top: 2.5rem; color: #64748b; font-size: .8rem; }
</style>
</head>
<body>
<main>
  <h1>Toolkit preview</h1>
  <p class="lead">Read-only. This page holds no key and cannot sign or broadcast a transaction.</p>

  <form method="get" action="">
    <div>
      <label for="network">Network</label>
      <select id="network" name="network">
        <?php foreach ($networks as $key => $value): ?>
          <option value="<?php echo h($key); ?>"<?php echo $key === $slug ? ' selected' : ''; ?>>
            <?php echo h($value['name']); ?>
          </option>
        <?php endforeach; ?>
      </select>
    </div>
    <div>
      <label for="addresses">Addresses (comma separated, optional)</label>
      <input id="addresses" name="addresses" value="<?php echo h($addressInput); ?>"
             placeholder="0x0000000000000000000000000000000000000000" />
    </div>
    <div>
      <label for="amount">Amount of <?php echo h($net['currency']); ?> each (optional)</label>
      <input id="amount" name="amount" value="<?php echo h($amount); ?>" placeholder="0.001" />
    </div>
    <button type="submit">Read the chain</button>
  </form>

  <?php if ($inputError !== ''): ?>
    <p class="err"><?php echo $inputError; ?></p>
  <?php endif; ?>

  <?php if ($rpcError !== ''): ?>
    <p class="err">RPC error: <?php echo h($rpcError); ?></p>
  <?php elseif ($chain !== null): ?>
    <table>
      <tr><th>Network</th><td><?php echo h($net['name']); ?><?php echo $net['testnet'] ? ' (testnet)' : ''; ?></td></tr>
      <tr><th>Chain id</th><td><code><?php echo (int) $chain['id']; ?></code>
        <?php echo $chain['id'] === $net['chain_id'] ? '' : ' <span class="warn">unexpected</span>'; ?></td></tr>
      <tr><th>Block</th><td><?php echo h(Wei::thousands((string) $chain['block'])); ?></td></tr>
      <tr><th>Gas price</th><td><?php echo h((string) $chain['gas_gwei']); ?> gwei</td></tr>
      <tr><th>RPC</th><td><code><?php echo h($net['rpc']); ?></code></td></tr>
      <tr><th>Database</th><td>
        <code><?php echo $db['on'] === 1 ? '01' : '00'; ?></code>
        <?php if ($db['connected'] && $db['table']): ?>
          connected, journal table ready
        <?php elseif ($db['connected']): ?>
          connected, but the journal table is not ready
        <?php elseif ($db['configured']): ?>
          configured but not reachable
        <?php else: ?>
          not configured (the file journal still works)
        <?php endif; ?>
      </td></tr>
    </table>

    <?php if ($balances !== array()): ?>
      <table>
        <tr><th>Address</th><th>Balance</th></tr>
        <?php foreach ($balances as $row): ?>
          <tr>
            <td><code><?php echo h($row['address']); ?></code></td>
            <td><?php echo h(pol($row['wei'])); ?> <?php echo h($net['currency']); ?></td>
          </tr>
        <?php endforeach; ?>
      </table>
    <?php endif; ?>

    <?php if ($plan !== null): ?>
      <table>
        <tr><th>Recipients</th><td><?php echo (int) $plan['count']; ?></td></tr>
        <tr><th>Each</th><td><?php echo h($plan['amount']); ?> <?php echo h($net['currency']); ?></td></tr>
        <tr><th>Value</th><td><?php echo h(pol($plan['value_wei'])); ?></td></tr>
        <tr><th>Fee at 21,000 gas each</th><td><?php echo h(pol($plan['fee_wei'])); ?></td></tr>
        <tr><th>Total required</th><td><?php echo h(pol($plan['total_wei'])); ?></td></tr>
      </table>
      <p class="warn">Assumes every recipient is a plain wallet with no code. An address with
        code costs more than 21,000 gas, so the real figure can be higher.</p>
    <?php endif; ?>
  <?php endif; ?>

  <?php $overview = db_overview(); ?>
  <?php if ($overview !== array()): ?>
    <h2 style="font-size:1rem;margin:2.25rem 0 .5rem;color:#f8fafc;">Databases on this account</h2>
    <table>
      <tr><th>Database</th><th>On</th><th>Tables</th><th>Rows (estimate)</th></tr>
      <?php foreach ($overview as $label => $entry): ?>
        <?php
        $rows = 0;
        foreach ($entry['tables'] as $table) {
            $rows += (int) $table['rows_estimate'];
        }
        ?>
        <tr>
          <td><code><?php echo h($label); ?></code><?php echo $label === 'toolkit' ? ' (this one)' : ''; ?></td>
          <td><code><?php echo $entry['on'] === 1 ? '01' : '00'; ?></code></td>
          <td><?php echo $entry['connected'] ? (int) $entry['table_count'] : h($entry['detail']); ?></td>
          <td><?php echo $entry['connected'] ? h(Wei::thousands((string) $rows)) : '&mdash;'; ?></td>
        </tr>
      <?php endforeach; ?>
    </table>
    <p class="warn">Row counts come from <code>information_schema</code> and are estimates for
      InnoDB, not exact counts. The other databases are read with <strong>SELECT only</strong>,
      and the journal table is created in this one alone.</p>
  <?php endif; ?>

  <?php $domains = domain_map(); $synergy = domain_synergy(); ?>
  <?php if ($domains !== array()): ?>
    <h2 style="font-size:1rem;margin:2.25rem 0 .5rem;color:#f8fafc;">Domains &rarr; databases</h2>
    <p class="<?php echo $synergy['all_on'] === 1 ? 'lead' : 'warn'; ?>" style="margin:.25rem 0 .75rem;">
      <code><?php
        $vector = (string) $synergy['vector'];
        // 2,046 bits is unreadable on one line, so long vectors show both ends
        // and their length; the full vector is still in preview.php's JSON.
        echo h(strlen($vector) > 24
            ? substr($vector, 0, 8) . '...' . substr($vector, -8) . ' (' . strlen($vector) . ' bits)'
            : $vector);
      ?></code>
      &mdash; first <code><?php echo (int) $synergy['first']; ?></code>,
      last <code><?php echo (int) $synergy['last']; ?></code>,
      <strong><?php echo (int) $synergy['connected']; ?> of <?php echo (int) $synergy['domains']; ?></strong> connected,
      all together: <code><?php echo $synergy['all_on'] === 1 ? '01' : '00'; ?></code>
    </p>
    <?php $defaultLabel = db_default_label(); ?>
    <?php if ($defaultLabel !== null): ?>
      <p class="lead" style="margin:.25rem 0 .75rem;">
        Every other domain on this account &mdash; including any added later &mdash; is attached to
        <code><?php echo h($defaultLabel); ?></code> by default.
      </p>
    <?php endif; ?>
    <table>
      <tr><th>Domain</th><th>On</th><th>Database</th><th>Tables</th></tr>
      <?php $shown = 0; foreach ($domains as $domain => $binding): ?>
        <?php if ($shown >= 50) { continue; } $shown++; ?>
        <tr>
          <td><code><?php echo h($domain); ?></code></td>
          <td><code><?php echo $binding['on'] === 1 ? '01' : '00'; ?></code></td>
          <td><?php echo $binding['database'] === null
              ? 'unbound (no such database label: ' . h($binding['label']) . ')'
              : '<code>' . h($binding['database']) . '</code>'; ?></td>
          <td><?php echo $binding['bound'] ? (int) $binding['tables'] : '&mdash;'; ?></td>
        </tr>
      <?php endforeach; ?>
      <?php if (count($domains) > $shown): ?>
        <tr><td colspan="4" class="lead">&hellip; and <?php echo (int) (count($domains) - $shown); ?> more,
          all bound the same way.</td></tr>
      <?php endif; ?>
    </table>
  <?php endif; ?>

  <footer>
    Port of <code>broadcast.py</code> + a read-only view. Distribution still runs locally,
    where the key lives.
  </footer>
</main>
</body>
</html>
