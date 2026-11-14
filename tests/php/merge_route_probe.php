<?php
declare(strict_types=1);

/**
 * Run merge_rows() for real, against a stub mysqli.
 *
 * merge.php holds the decision; db_sync.php holds the route -- the connection,
 * the transaction, the prepared statement and the statuses. That route half had
 * only ever run on a host, because the file executes on include. It is included
 * here as a library instead (SYNC_LIBRARY, so the dispatch at the end returns and
 * refuse() throws its status), and merge_rows() takes the connection from us.
 *
 * The stub is a `mysqli` shaped class, not a mock of the writer: merge_rows
 * builds the real MysqliMergeWriter, prepares the real statement through
 * mysqli_prepare, binds with the real mysqli_stmt_bind_param, and the stub is
 * what answers. So what runs is the code that would run on the host.
 *
 * The stub models ON DUPLICATE KEY UPDATE the way merge.php states it: seen_in
 * keeps the larger count, first_seen is never rewritten, and the run token names
 * the last writer. That is the intent, not MySQL's evaluation of it -- only a
 * server can prove GREATEST agrees. What this proves is that the route sends one
 * statement whose placeholders match its bind types and its parameters, that the
 * columns line up with the values, that a repeat run updates instead of adding,
 * that every row carries the run token, and what it answers when it refuses.
 *
 *     php -n tests/php/merge_route_probe.php
 *
 * -n because the stub only exists where the mysqli extension does not: with it
 * loaded, `class mysqli` cannot be declared and these functions already exist.
 */

if (extension_loaded('mysqli')) {
    echo json_encode(array('unavailable' => 'the mysqli extension is loaded, so the stub cannot stand in')), "\n";
    exit(2);
}

define('SYNC_LIBRARY', true);

require_once __DIR__ . '/../../php/db_sync.php';

require_once __DIR__ . '/mysqli_stub.php';

/** One merge, the way the route runs it: a connection, a token, a result. */
function probe_case(array $rows, ?string $run, ?mysqli $link = null): array
{
    $link = $link ?? new mysqli();

    try {
        $response = merge_rows($rows, $run, $link);
        $stored = $link->stored();

        return array(
            'ok' => true,
            'status' => 200,
            'response' => $response,
            'stored' => $stored,
            'keys' => array_keys($stored),
            'began' => $link->began,
            'committed' => $link->committed,
            'rolled_back' => $link->rolledBack,
            'prepared' => $link->prepared,
            'binds' => $link->binds,
            'disagreements' => $link->disagreements,
        );
    } catch (SyncRefused $refused) {
        $stored = $link->stored();

        return array(
            'ok' => false,
            'status' => $refused->status,
            'message' => $refused->getMessage(),
            'stored' => $stored,
            'keys' => array_keys($stored),
            'began' => $link->began,
            'committed' => $link->committed,
            'rolled_back' => $link->rolledBack,
            'prepared' => $link->prepared,
            'binds' => $link->binds,
            'disagreements' => $link->disagreements,
        );
    }
}

$run1 = 'run1' . str_repeat('a', 28);
$run2 = 'run2' . str_repeat('b', 28);

$rows = array(
    array('table' => 'broadcast_journal', 'values' => array('id' => 1, 'hash' => '0xaa'), 'seen' => 2),
    array('table' => 'cjd_source', 'values' => array('filename' => 'cjd.php', 'bytes' => 65154), 'seen' => 9999),
    array('table' => 'cjd_source', 'values' => array('ratio' => 1.0, 'note' => 'ü')),
);

$connection = new mysqli();
$first = probe_case($rows, $run1, $connection);
$second = probe_case($rows, $run2, $connection);

$refusals = array(
    'bad table name' => array(array(array('table' => 'bad name; drop', 'values' => array('a' => 1))), 400),
    'no values' => array(array(array('table' => 't', 'values' => array())), 400),
    'bad source_db' => array(
        array(array('table' => 't', 'values' => array('a' => 1), 'source_db' => 'no such db')),
        400,
    ),
    'key mismatch' => array(
        array(array('table' => 't', 'values' => array('a' => 1), 'key' => str_repeat('0', 64))),
        422,
    ),
    'not an object' => array(array('just a string'), 400),
);

$refused = array();
foreach ($refusals as $name => $case) {
    $refused[$name] = probe_case($case[0], $run1);
}

$broken = new mysqli();
$broken->failColumns = true;
$tableFailure = probe_case($rows, $run1, $broken);

$sql = $first['prepared'][0] ?? '';
$report = array(
    'run1' => $run1,
    'run2' => $run2,
    'sql' => $sql,
    'shape' => stub_shape($sql),
    'first' => $first,
    'second' => $second,
    'refusals' => $refused,
    'expected_statuses' => array_map(static fn ($case) => $case[1], $refusals),
    'table_failure' => $tableFailure,
);

echo json_encode($report, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE), "\n";
