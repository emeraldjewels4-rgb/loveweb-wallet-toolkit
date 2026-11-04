<?php
declare(strict_types=1);

/**
 * Read-only chain preview. Returns JSON; signs nothing and holds no key.
 *
 *   GET preview.php?network=amoy
 *   GET preview.php?network=amoy&addresses=0xabc...,0xdef...&amount=0.001
 *
 * Everything here is a read: eth_chainId, eth_blockNumber, eth_gasPrice,
 * eth_getBalance. There is deliberately no eth_sendRawTransaction route -- a
 * web-accessible sender would need a key on the host, and that is the one thing
 * this toolkit never does.
 */

require_once __DIR__ . '/broadcast.php';
require_once __DIR__ . '/db.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

$networks = array(
    'amoy' => array(
        'name' => 'Polygon Amoy testnet',
        'chain_id' => 80002,
        'rpc' => 'https://polygon-amoy-bor-rpc.publicnode.com',
        'currency' => 'POL',
        'explorer' => 'https://amoy.polygonscan.com',
        'testnet' => true,
    ),
    'polygon' => array(
        'name' => 'Polygon PoS mainnet',
        'chain_id' => 137,
        'rpc' => 'https://polygon-bor-rpc.publicnode.com',
        'currency' => 'POL',
        'explorer' => 'https://polygonscan.com',
        'testnet' => false,
    ),
);

$slug = isset($_GET['network']) ? strtolower((string) $_GET['network']) : 'amoy';
if (!isset($networks[$slug])) {
    http_response_code(400);
    echo json_encode(array('error' => 'unknown network; use one of: ' . implode(', ', array_keys($networks))));
    exit;
}
$net = $networks[$slug];

$addresses = array();
if (isset($_GET['addresses'])) {
    foreach (explode(',', (string) $_GET['addresses']) as $candidate) {
        $candidate = trim($candidate);
        if ($candidate === '') {
            continue;
        }
        if (!preg_match('/^0x[0-9a-fA-F]{40}$/', $candidate)) {
            http_response_code(400);
            echo json_encode(array('error' => 'not an address: ' . substr($candidate, 0, 60)));
            exit;
        }
        $addresses[] = strtolower($candidate);
        if (count($addresses) >= 50) {
            break;
        }
    }
}

$amount = isset($_GET['amount']) ? (string) $_GET['amount'] : '0';
if (!preg_match('/^[0-9]+(\.[0-9]+)?$/', $amount)) {
    http_response_code(400);
    echo json_encode(array('error' => 'amount must be a plain decimal number'));
    exit;
}

$rpc = new JsonRpc($net['rpc'], 20);
$response = array(
    'network' => $slug,
    'name' => $net['name'],
    'testnet' => $net['testnet'],
    'explorer' => $net['explorer'],
    'currency' => $net['currency'],
    'rpc' => $net['rpc'],
    'signs' => false,
    'note' => 'read-only: this endpoint cannot sign or broadcast',
    // Health only: no DSN, no credentials, and saying "not configured" is fine.
    'database' => db_status(),
    // Every database on the account: connected, plus table counts. Read-only.
    'databases' => array_map(
        function (array $entry): array {
            return array('on' => $entry['on'], 'connected' => $entry['connected'], 'tables' => $entry['table_count']);
        },
        db_overview()
    ),
    // Which database each hosted domain is bound to, and whether it is on.
    'domains' => domain_map(),
    // All of them at once: first/last integer of the vector, and all_on.
    'synergy' => domain_synergy(),
    // The stored text file, by size and hash only -- never the body.
    'cjd' => cjd_status(),
);

try {
    $chainId = $rpc->call('eth_chainId');
    $blockHex = $rpc->call('eth_blockNumber');
    $gasPriceHex = $rpc->call('eth_gasPrice');

    $chainIdInt = hexdec((string) $chainId);
    $blockInt = hexdec((string) $blockHex);
    $gasPriceWei = Wei::fromHex((string) $gasPriceHex);

    $response['chain_id'] = $chainIdInt;
    $response['chain_id_matches'] = ($chainIdInt === $net['chain_id']);
    $response['block'] = $blockInt;
    $response['gas_price_gwei'] = Wei::scale($gasPriceWei, 9, 3);

    if ($addresses !== array()) {
        $balances = array();
        foreach ($addresses as $address) {
            $balanceHex = $rpc->call('eth_getBalance', array($address, 'latest'));
            // Exact string math: a balance above ~9.2 POL overflows a PHP int.
            $wei = Wei::fromHex((string) $balanceHex);
            $balances[] = array(
                'address' => $address,
                'wei' => $wei,
                'eth' => Wei::scale($wei, 18, 6),
                'explorer' => $net['explorer'] . '/address/' . $address,
            );
        }
        $response['balances'] = $balances;
    }

    if ($amount !== '0' && $addresses !== array()) {
        // A value transfer to an address with no code costs exactly 21,000 gas.
        $count = count($addresses);
        $gas = 21000 * $count;
        $units = Wei::parseAmount($amount);
        $valueWei = Wei::mulSmall($units, $count);
        $feeWei = Wei::mulSmall($gasPriceWei, $gas);
        $totalWei = Wei::add($valueWei, $feeWei);
        $response['plan'] = array(
            'recipients' => $count,
            'amount_each' => $amount,
            'total_value_wei' => $valueWei,
            'gas_units' => $gas,
            'fee_wei' => $feeWei,
            'fee_eth' => Wei::scale($feeWei, 18, 6),
            'total_required_eth' => Wei::scale($totalWei, 18, 6),
            'assumes' => 'a plain wallet with no code; an address with code costs more and needs an estimate',
        );
    }
} catch (RpcError $exc) {
    http_response_code(502);
    $response['error'] = 'rpc: ' . $exc->getMessage();
}

echo json_encode($response, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
