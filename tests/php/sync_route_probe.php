<?php
declare(strict_types=1);

/**
 * Run the route's dispatch for real, with no request and no host.
 *
 * sync_route() takes the request as data -- method, query, the key header, the
 * body -- so every refusal the route can answer with is reachable in-process:
 * no key installed, a wrong key, a GET where a merge would write, an empty or
 * malformed body, more rows than one request carries, an unknown action. Under
 * SYNC_LIBRARY, refuse() throws instead of exiting, so the status arrives as
 * data rather than as a dead process.
 *
 * Each case gets its own stub connection, so "nothing was written" is a fact
 * about that case and not about whether an earlier one wrote: the guard could
 * refuse before the merge for the wrong reason and this would not notice, which
 * is why the messages are asserted too.
 *
 * The key is written by this probe to a temporary file: the host's import.key is
 * neither needed nor read, and no key material appears in this file.
 *
 *     php -n tests/php/sync_route_probe.php
 */

if (extension_loaded('mysqli')) {
    echo json_encode(array('unavailable' => 'the mysqli extension is loaded, so the stub cannot stand in')), "\n";
    exit(2);
}

define('SYNC_LIBRARY', true);

require_once __DIR__ . '/../../php/db_sync.php';
require_once __DIR__ . '/mysqli_stub.php';

/** A key this probe made up, on disk for the length of this run. */
$keyFile = (string) tempnam(sys_get_temp_dir(), 'sync_route_key_');
file_put_contents($keyFile, "probe-key-0123456789abcdef\n");
$missingFile = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'sync_route_no_key_' . getmypid();

/**
 * One request through sync_route(), and what it left in the stub.
 *
 * @param array<string,mixed> $request
 * @return array<string,mixed>
 */
function route_case(array $request, ?string $keyFile, ?mysqli $link = null): array
{
    $link = $link ?? new mysqli();

    try {
        $payload = sync_route($request, $keyFile, $link);

        return array(
            'ok' => true,
            'status' => 200,
            'payload' => $payload,
            'written' => count($link->stored()),
            'stored' => $link->stored(),
            'committed' => $link->committed,
            'rolled_back' => $link->rolledBack,
        );
    } catch (SyncRefused $refused) {
        return array(
            'ok' => false,
            'status' => $refused->status,
            'message' => $refused->getMessage(),
            'written' => count($link->stored()),
            'stored' => $link->stored(),
            'committed' => $link->committed,
            'rolled_back' => $link->rolledBack,
        );
    }
}

/** A request that is right in every way the guard checks, so only one thing is wrong. */
function request(array $overrides = array()): array
{
    return array_merge(
        array(
            'method' => 'POST',
            'query' => array('action' => 'merge'),
            'header_key' => 'probe-key-0123456789abcdef',
            'body' => json_encode(array(
                'rows' => array(array('table' => 'broadcast_journal', 'values' => array('id' => 1))),
                'run' => str_repeat('r', 32),
            )),
        ),
        $overrides
    );
}

$rows = array();
for ($index = 0; $index < 50001; $index++) {
    $rows[] = array('table' => 't', 'values' => array('a' => $index));
}

$cases = array(
    // The guard, and the two ways a caller can present a key.
    'no key installed' => route_case(request(), $missingFile),
    'wrong key' => route_case(request(array('header_key' => 'not the key')), $keyFile),
    'key in the header' => route_case(request(), $keyFile),
    'key in the query' => route_case(
        request(array('method' => 'GET', 'header_key' => null,
                      'query' => array('action' => 'status', 'key' => 'probe-key-0123456789abcdef'))),
        $keyFile
    ),

    // A GET that would write.
    'GET merge' => route_case(request(array('method' => 'GET')), $keyFile),

    // The body.
    'empty body' => route_case(request(array('body' => '')), $keyFile),
    'malformed body' => route_case(request(array('body' => 'not json at all')), $keyFile),
    'rows not a list' => route_case(request(array('body' => '{"rows": 5}')), $keyFile),
    'no rows' => route_case(request(array('body' => '{"rows": []}')), $keyFile),
    'too many rows' => route_case(request(array('body' => json_encode(array('rows' => $rows)))), $keyFile),

    // The run token, in both places it can arrive.
    'bad run token' => route_case(
        request(array('body' => json_encode(array('rows' => array(array('table' => 't', 'values' => array('a' => 1))), 'run' => 'no spaces please')))),
        $keyFile
    ),
    'bad run token in status' => route_case(
        request(array('method' => 'GET', 'query' => array('action' => 'status', 'run' => 'x'))),
        $keyFile
    ),

    // Anything else.
    'unknown action' => route_case(
        request(array('method' => 'GET', 'query' => array('action' => 'nonsense'))),
        $keyFile
    ),

    // And the action that does write, so the refusals above are refusals.
    'merge' => route_case(request(), $keyFile),
    'merge without a run token' => route_case(
        request(array('body' => json_encode(array('rows' => array(array('table' => 't', 'values' => array('a' => 2))))))),
        $keyFile
    ),
);

@unlink($keyFile);

echo json_encode(array('cases' => $cases), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE), "\n";
