<?php
declare(strict_types=1);

/**
 * Take everything out of the databases on this host, and put the merged result
 * back into one table.
 *
 *   GET  db_sync.php?action=export&limit=20000   (read)   every table, every database
 *   POST db_sync.php?action=merge                (write)  rows -> merged_rows
 *   GET  db_sync.php?action=status[&run=token]   (read)   the table, or one upload
 *   GET  db_sync.php?action=report               (read)   what each database is running on
 *
 * Why a route at all: there is no shell and no remote MySQL on this account, so
 * the only code that can read the databases is PHP served from this directory.
 * A download therefore has to be a request, and the "download" here is JSON that
 * sync_database.py saves locally and merges.
 *
 * Why merge into a table instead of rewriting anything: the account has several
 * databases and this user has SELECT-only grants on the neighbours, which is the
 * right arrangement -- a tool that can rewrite another application's tables is a
 * tool that eventually does. So the neighbours are read, and their rows are
 * consolidated into `merged_rows` in the primary database. Nothing is deleted
 * and no neighbour is ever written to. Because nothing is deleted, the merge is
 * repeatable: the same row merges to the same key, and a second run updates
 * last_seen instead of inserting a copy.
 *
 * The key that identifies a row is computed here, from the table name and the
 * row's values, never by the caller. A caller-supplied key is checked against
 * the one this side computes and the request is refused when they disagree, so
 * a row cannot be filed under a key that does not describe it.
 *
 * is a write, so it is guarded exactly like cjd_store.php: no import.key on the
 * host means the route answers 503, a wrong key means 403, and the key is never
 * echoed back.
 */

require_once __DIR__ . '/db.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

/** Rows per table in one export. A cap, not a hope: the reply says when it bit. */
const SYNC_ROW_CAP = 20000;

/** The import key, if one was installed on the host. Never echoed. */
function sync_key(): ?string
{
    $path = __DIR__ . '/import.key';
    if (!is_file($path) || !is_readable($path)) {
        return null;
    }
    $key = trim((string) file_get_contents($path));

    return $key === '' ? null : $key;
}

/** @return never */
function refuse(int $code, string $message): void
{
    http_response_code($code);
    echo json_encode(array('error' => $message), JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

/**
 * The identity of one row: SHA-256 over the table name and its values.
 *
 * The table name is in the key so a row in `journal` is not the same row as an
 * identical one in `ledger`; the database name is deliberately NOT in it, so the
 * same row found in two databases merges to one row rather than two.
 *
 * Keys are sorted first: two databases return the same columns in whatever order
 * their schema declares, and unsorted, that difference alone would look like two
 * different rows.
 *
 * @param array<string,mixed> $values
 */
function row_key(string $table, array $values): string
{
    ksort($values);

    return hash('sha256', $table . "\n" . json_encode($values, JSON_UNESCAPED_SLASHES));
}

/**
 * A value that survives the trip as JSON.
 *
 * mysqli hands back strings whatever the column type. Text in utf8mb4 encodes
 * as-is; anything that is not valid UTF-8 (a BLOB, a latin1 column, a stray
 * byte) would be mangled by json_encode, so it travels base64-wrapped and is
 * unwrapped by whoever reads it. Silent corruption is the failure this exists to
 * prevent.
 *
 * @param mixed $value
 * @return mixed
 */
function encode_value($value)
{
    if ($value === null) {
        return null;
    }
    $text = (string) $value;
    if (preg_match('//u', $text) === 1) {
        return $text;
    }

    return array('b64' => base64_encode($text));
}

/** @return array<int,string> */
function table_names(mysqli $link, string $database): array
{
    $names = array();
    $statement = mysqli_prepare(
        $link,
        "SELECT TABLE_NAME FROM information_schema.TABLES"
        . " WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
    );
    mysqli_stmt_bind_param($statement, 's', $database);
    mysqli_stmt_execute($statement);
    $result = mysqli_stmt_get_result($statement);
    while ($row = mysqli_fetch_row($result)) {
        $names[] = (string) $row[0];
    }
    mysqli_stmt_close($statement);

    return $names;
}

/** @return array<int,string> */
function column_names(mysqli $link, string $database, string $table): array
{
    $columns = array();
    $statement = mysqli_prepare(
        $link,
        'SELECT COLUMN_NAME FROM information_schema.COLUMNS'
        . ' WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION'
    );
    mysqli_stmt_bind_param($statement, 'ss', $database, $table);
    mysqli_stmt_execute($statement);
    $result = mysqli_stmt_get_result($statement);
    while ($row = mysqli_fetch_row($result)) {
        $columns[] = (string) $row[0];
    }
    mysqli_stmt_close($statement);

    return $columns;
}

/** A table name quoted for SQL. Names come from information_schema, not a query string. */
function quote_identifier(string $name): string
{
    return '`' . str_replace('`', '``', $name) . '`';
}

/**
 * Every configured database, every table, and the rows in it.
 *
 * The cap is applied per table and re-fetched as `cap + 1` rows, so "is there
 * more?" is answered by the database rather than assumed: a truncated table says
 * so in the reply instead of quietly exporting a prefix.
 *
 * @return array<string,mixed>
 */
function export_all(int $cap): array
{
    $tables = array();
    $rows = 0;

    foreach (db_labels() as $label => $name) {
        $link = db_named($label);
        if ($link === null) {
            $tables[] = array('database' => $label, 'table' => null, 'on' => 0, 'error' => 'not reachable');
            continue;
        }

        try {
            $names = table_names($link, $name);
        } catch (Throwable $exc) {
            $tables[] = array('database' => $label, 'table' => null, 'on' => 0, 'error' => 'connected, tables not readable');
            continue;
        }

        foreach ($names as $table) {
            // The merged table is this route's own output. Exporting it would
            // make every run merge its own previous merge, so it is skipped.
            if ($label === DB_PRIMARY && $table === 'merged_rows') {
                continue;
            }

            $entry = array(
                'database' => $label,
                'table' => $table,
                'on' => 1,
                'columns' => array(),
                'rows' => array(),
                'truncated' => false,
                'error' => null,
            );

            try {
                $entry['columns'] = column_names($link, $name, $table);
                $result = mysqli_query($link, 'SELECT * FROM ' . quote_identifier($table) . ' LIMIT ' . ($cap + 1));
                if ($result === false) {
                    throw new RuntimeException('select refused');
                }
                $fetched = 0;
                while ($assoc = mysqli_fetch_assoc($result)) {
                    if ($fetched >= $cap) {
                        $entry['truncated'] = true;
                        break;
                    }
                    $fetched++;

                    $values = array();
                    foreach ($assoc as $column => $value) {
                        $values[(string) $column] = encode_value($value);
                    }
                    $json = json_encode($values, JSON_UNESCAPED_SLASHES);
                    $entry['rows'][] = array(
                        'key' => row_key($table, $values),
                        'bytes' => $json === false ? 0 : strlen($json),
                        'values' => $values,
                    );
                }
                mysqli_free_result($result);
                $rows += $fetched;
            } catch (Throwable $exc) {
                $entry['error'] = 'rows not readable';
            }

            $tables[] = $entry;
        }
    }

    return array(
        'action' => 'export',
        'generated_at' => gmdate('c'),
        'cap' => $cap,
        'tables' => $tables,
        'rows' => $rows,
        'merged' => merged_status(),
    );
}

/**
 * Merge rows into the primary database's merged_rows table.
 *
 * One transaction for the whole request: a merge that half landed would leave a
 * table whose row count matches nothing, and the caller's check would fail for
 * a reason that is not visible from outside.
 *
 * @param array<int,mixed> $rows
 * @param string|null $run token naming this upload; stamped on every row it
 *                          writes, so the caller can prove "what I sent is
 *                          stored" without the table ever having to shrink
 * @return array<string,mixed>
 */
function merge_rows(array $rows, ?string $run = null): array
{
    $link = db();
    if ($link === null) {
        refuse(503, 'database not reachable');
    }

    try {
        ensure_merged_table($link);
    } catch (Throwable $exc) {
        refuse(503, 'could not prepare the merged_rows table');
    }

    $accepted = 0;
    $keys = array();

    mysqli_begin_transaction($link);
    try {
        $statement = mysqli_prepare(
            $link,
            'INSERT INTO merged_rows'
            .            ' (key_sha256, source_table, source_db, seen_in, row_bytes, row_json, first_seen, last_seen, run_token)'
            . ' VALUES (?, ?, ?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP(), ?)'
            . ' ON DUPLICATE KEY UPDATE'
            . ' seen_in = GREATEST(seen_in, VALUES(seen_in)),'
            . ' row_bytes = VALUES(row_bytes),'
            . ' row_json = VALUES(row_json),'
            . ' source_db = VALUES(source_db),'
            . ' run_token = VALUES(run_token),'
            . ' last_seen = UTC_TIMESTAMP()'
        );

        foreach ($rows as $row) {
            if (!is_array($row)) {
                refuse(400, 'every row must be an object');
            }
            $table = isset($row['table']) ? (string) $row['table'] : '';
            if (preg_match('/^[A-Za-z0-9_]{1,64}$/', $table) !== 1) {
                refuse(400, 'row table name is not a plain identifier');
            }
            $values = isset($row['values']) && is_array($row['values']) ? $row['values'] : array();
            if ($values === array()) {
                refuse(400, 'a row without values describes nothing');
            }
            $database = isset($row['source_db']) ? (string) $row['source_db'] : DB_PRIMARY;
            if (preg_match('/^[A-Za-z0-9_]{1,64}$/', $database) !== 1) {
                refuse(400, 'row source_db is not a configured label');
            }

            $key = row_key($table, $values);
            if (isset($row['key']) && !hash_equals($key, (string) $row['key'])) {
                refuse(422, 'a row arrived under a key that does not describe it');
            }

            $json = json_encode($values, JSON_UNESCAPED_SLASHES);
            if ($json === false) {
                refuse(422, 'a row could not be encoded');
            }

            $seen = isset($row['seen']) ? max(1, min(1000, (int) $row['seen'])) : 1;
            $bytes = strlen($json);

            mysqli_stmt_bind_param($statement, 'sssiiss', $key, $table, $database, $seen, $bytes, $json, $run);
            mysqli_stmt_execute($statement);

            $accepted++;
            $keys[$key] = true;
        }

        mysqli_stmt_close($statement);
        mysqli_commit($link);
    } catch (Throwable $exc) {
        mysqli_rollback($link);
        // Never the exception text: it can name tables and, on some drivers, the
        // connection. The caller gets a status, not a schema.
        refuse(500, 'the merge was refused by the database');
    }

    return array(
        'action' => 'merge',
        'received' => count($rows),
        'accepted' => $accepted,
        'distinct' => count($keys),
        'on' => 1,
        'run_token' => $run,
        'merged' => merged_status($run),
    );
}

/**
 * One column, one row, or null. Every query here is a read of the server itself.
 */
function scalar(mysqli $link, string $sql): ?string
{
    try {
        $result = mysqli_query($link, $sql);
        if ($result === false) {
            return null;
        }
        $row = mysqli_fetch_row($result);
        mysqli_free_result($result);

        return is_array($row) ? (string) $row[0] : null;
    } catch (Throwable $exc) {
        return null;
    }
}

/**
 * The TLS cipher in use, or '' when the connection is not encrypted.
 *
 * Empty is the normal answer on a host that reaches MySQL over the local socket,
 * and it is worth stating: "no TLS" is a fact about the connection, not a fault.
 */
function tls_cipher(mysqli $link): ?string
{
    try {
        $result = mysqli_query($link, "SHOW STATUS LIKE 'Ssl_cipher'");
        if ($result !== false) {
            $row = mysqli_fetch_row($result);
            mysqli_free_result($result);
            if (is_array($row)) {
                return (string) ($row[1] ?? '');
            }
        }
    } catch (Throwable $exc) {
        return null;
    }

    return null;
}

/**
 * What each configured database is running on, and what this user may do to it.
 *
 * Everything is a read -- no CREATE, no INSERT, not even a "try a write and see".
 * The question is "what would have to change before this database could hold
 * anything", and the answers are the things a connection test hides:
 *
 *   - the server version, because utf8mb4 needs 5.5.3+ and CHECK constraints 10.2+;
 *   - each table's storage engine, because MyISAM has no transactions;
 *   - the database's own default charset, because a table can be utf8mb4 while
 *     the schema it lives in is not;
 *   - the privileges actually held, from SHOW GRANTS rather than inferred from
 *     "it connected, so it must work" -- those are different claims, and only one
 *     of them is checkable.
 *
 * @return array<string,mixed>
 */
function database_report(): array
{
    $report = array('generated_at' => gmdate('c'), 'primary' => DB_PRIMARY, 'databases' => array());

    foreach (db_labels() as $label => $name) {
        $entry = array(
            'label' => $label,
            'database' => $name,
            'on' => 0,
            'connected' => false,
            'detail' => 'not reachable',
            'round_trip_ms' => null,
            'server' => array(),
            'database_defaults' => array('charset' => null, 'collation' => null),
            'tables' => array(),
            'privileges' => array(),
            'tls' => null,
            'features' => array(),
        );

        $link = db_named($label);
        if ($link === null) {
            $report['databases'][$label] = $entry;
            continue;
        }

        $entry['on'] = 1;
        $entry['connected'] = true;
        $entry['detail'] = 'connected';

        try {
            $start = microtime(true);
            mysqli_query($link, 'SELECT 1');
            $entry['round_trip_ms'] = round((microtime(true) - $start) * 1000, 1);
        } catch (Throwable $exc) {
            $entry['detail'] = 'connected, then refused a query';
        }

        $entry['server'] = array(
            'version' => scalar($link, 'SELECT VERSION()'),
            'comment' => scalar($link, 'SELECT @@version_comment'),
            'protocol' => mysqli_get_proto_info($link),
            'charset_server' => scalar($link, 'SELECT @@character_set_server'),
            'collation_server' => scalar($link, 'SELECT @@collation_server'),
            'max_allowed_packet' => scalar($link, 'SELECT @@max_allowed_packet'),
            'wait_timeout' => scalar($link, 'SELECT @@wait_timeout'),
            'sql_mode' => scalar($link, 'SELECT @@sql_mode'),
            'host_info' => mysqli_get_host_info($link),
        );

        $entry['features'] = array(
            'utf8mb4' => scalar(
                $link,
                "SELECT COUNT(*) FROM information_schema.CHARACTER_SETS WHERE CHARACTER_SET_NAME = 'utf8mb4'"
            ) === '1',
            'innodb' => scalar($link, "SELECT SUPPORT FROM information_schema.ENGINES WHERE ENGINE = 'InnoDB'"),
            'foreign_keys' => scalar($link, 'SELECT @@foreign_key_checks'),
        );

        $entry['tls'] = tls_cipher($link);

        try {
            $statement = mysqli_prepare(
                $link,
                'SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME'
                . ' FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = ?'
            );
            mysqli_stmt_bind_param($statement, 's', $name);
            mysqli_stmt_execute($statement);
            $result = mysqli_stmt_get_result($statement);
            $row = mysqli_fetch_assoc($result);
            mysqli_stmt_close($statement);
            if (is_array($row)) {
                $entry['database_defaults'] = array(
                    'charset' => (string) $row['DEFAULT_CHARACTER_SET_NAME'],
                    'collation' => (string) $row['DEFAULT_COLLATION_NAME'],
                );
            }
        } catch (Throwable $exc) {
            $entry['database_defaults'] = array('charset' => null, 'collation' => null, 'error' => 'schema not readable');
        }

        try {
            $statement = mysqli_prepare(
                $link,
                'SELECT TABLE_NAME, ENGINE, TABLE_COLLATION, COALESCE(TABLE_ROWS, 0) AS row_estimate,'
                . " COALESCE(DATA_LENGTH, 0) AS data_bytes FROM information_schema.TABLES"
                . " WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
            );
            mysqli_stmt_bind_param($statement, 's', $name);
            mysqli_stmt_execute($statement);
            $result = mysqli_stmt_get_result($statement);
            while ($row = mysqli_fetch_assoc($result)) {
                $entry['tables'][] = array(
                    'name' => (string) $row['TABLE_NAME'],
                    'engine' => $row['ENGINE'] === null ? null : (string) $row['ENGINE'],
                    'collation' => $row['TABLE_COLLATION'] === null ? null : (string) $row['TABLE_COLLATION'],
                    'row_estimate' => (int) $row['row_estimate'],
                    'data_bytes' => (int) $row['data_bytes'],
                );
            }
            mysqli_stmt_close($statement);
        } catch (Throwable $exc) {
            // Reachable but not readable: no SELECT on the schema.
            $entry['detail'] = 'connected, schema not readable';
        }

        // Privileges from SHOW GRANTS: the only answer that is evidence rather
        // than an inference. GRANT lines never carry a password.
        $global = array();
        $scoped = array();
        $objects = array();
        $grantError = null;
        try {
            $result = mysqli_query($link, 'SHOW GRANTS');
            if ($result === false) {
                throw new RuntimeException('refused');
            }
            while ($row = mysqli_fetch_row($result)) {
                if (preg_match('/^GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+/i', (string) $row[0], $match) !== 1) {
                    continue;
                }
                $privileges = array_map('trim', explode(',', $match[1]));

                // MariaDB prints a database name in GRANT with its underscores
                // escaped (\`fiatgodx\_toolkit\`.*), so a plain comparison against
                // the database name never matches and every grant silently reads
                // as "USAGE only". Normalised here, once, for the whole route.
                $target = str_replace(array('\\', '`'), '', trim($match[2]));
                $objects[$target] = array_values(array_unique(
                    array_merge(isset($objects[$target]) ? $objects[$target] : array(), $privileges)
                ));

                $sink = null;
                if ($target === '*.*') {
                    $sink = 'global';
                } elseif ($target === $name . '.*') {
                    $sink = 'scoped';
                }
                if ($sink === null) {
                    continue;
                }
                $current = $sink === 'global' ? $global : $scoped;
                ${$sink} = array_values(array_unique(array_merge($current, $privileges)));
            }
            mysqli_free_result($result);
        } catch (Throwable $exc) {
            $grantError = 'SHOW GRANTS refused';
        }

        $held = array_values(array_unique(array_merge($global, $scoped)));
        $has = static function (string $privilege) use ($held): bool {
            return in_array('ALL PRIVILEGES', $held, true) || in_array($privilege, $held, true);
        };

        $entry['privileges'] = array(
            'global' => $global,
            'database' => $scoped,
            // Every GRANT target this user holds, normalised (db => privileges),
            // so nothing is dropped just because it was not the database asked about.
            'objects' => $objects,
            'read' => $has('SELECT'),
            'write' => $has('INSERT') && $has('UPDATE'),
            'create' => $has('CREATE'),
            'error' => $grantError,
        );

        $report['databases'][$label] = $entry;
    }

    return array(
        'action' => 'report',
        'generated_at' => $report['generated_at'],
        'primary' => $report['primary'],
        'databases' => $report['databases'],
        'php' => array(
            'version' => PHP_VERSION,
            'driver' => function_exists('mysqli_connect') ? 'mysqli' : 'none',
            'client' => defined('MYSQLI_CLIENT_SSL') ? mysqli_get_client_info() : null,
        ),
    );
}

$key = sync_key();
if ($key === null) {
    refuse(503, 'sync disabled: no import.key on the host');
}

$given = '';
if (isset($_SERVER['HTTP_X_IMPORT_KEY'])) {
    $given = (string) $_SERVER['HTTP_X_IMPORT_KEY'];
} elseif (isset($_GET['key'])) {
    $given = (string) $_GET['key'];
}
if (!hash_equals($key, $given)) {
    refuse(403, 'forbidden');
}

$action = isset($_GET['action']) ? strtolower((string) $_GET['action']) : 'export';

if ($action === 'status') {
    $run = isset($_GET['run']) ? (string) $_GET['run'] : null;
    if ($run !== null && preg_match('/^[A-Za-z0-9_-]{8,64}$/', $run) !== 1) {
        refuse(400, 'run token is not a plain token');
    }

    echo json_encode(array('action' => 'status', 'merged' => merged_status($run)), JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

if ($action === 'report') {
    echo json_encode(database_report(), JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

if ($action === 'export') {
    $cap = isset($_GET['limit']) ? (int) $_GET['limit'] : SYNC_ROW_CAP;
    $cap = max(1, min(50000, $cap));

    echo json_encode(export_all($cap), JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

if ($action === 'merge') {
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
        refuse(405, 'merge is a POST: a GET that could write is a link that could write');
    }

    $body = (string) file_get_contents('php://input');
    if ($body === '') {
        refuse(400, 'an empty body has nothing to merge');
    }

    $payload = json_decode($body, true);
    if (!is_array($payload) || !isset($payload['rows']) || !is_array($payload['rows'])) {
        refuse(400, 'expected {"rows": [...]}');
    }
    if ($payload['rows'] === array()) {
        refuse(400, 'no rows to merge');
    }
    if (count($payload['rows']) > 50000) {
        refuse(413, 'too many rows for one request: split the batch');
    }

    $run = isset($payload['run']) ? (string) $payload['run'] : null;
    if ($run !== null && preg_match('/^[A-Za-z0-9_-]{8,64}$/', $run) !== 1) {
        refuse(400, 'run token is not a plain token');
    }

    echo json_encode(merge_rows($payload['rows'], $run), JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
    exit;
}

refuse(400, 'unknown action; use export, merge, report or status');
