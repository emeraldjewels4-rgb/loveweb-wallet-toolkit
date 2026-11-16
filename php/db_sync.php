<?php
declare(strict_types=1);

/**
 * Take everything out of the databases on this host, and put the merged result
 * back into one table.
 *
 *   GET  db_sync.php?action=tables                 (read)   every table, no rows
 *   GET  db_sync.php?action=export&table=t.t&after=K&limit=N (read)  one page of one
 *                                                       table, ordered by its key
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
require_once __DIR__ . '/keys.php';
require_once __DIR__ . '/merge.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

/** Rows per page of one table: about a megabyte of JSON, so a page stays a page. */
const SYNC_PAGE_ROWS = 2000;

/** The largest page this route will serve, however the caller asks. */
const SYNC_PAGE_MAX = 20000;

/**
 * The import key, if one was installed on the host. Never echoed.
 *
 * The path is a parameter so a probe can point at a key it wrote, or at one that
 * is not there, without touching (or needing) the host's file.
 */
function sync_key(string $path): ?string
{
    if (!is_file($path) || !is_readable($path)) {
        return null;
    }
    $key = trim((string) file_get_contents($path));

    return $key === '' ? null : $key;
}

/** A refusal with a status, so a harness can catch what the route would answer. */
final class SyncRefused extends RuntimeException
{
    public int $status;

    public function __construct(int $code, string $message)
    {
        parent::__construct($message);
        $this->status = $code;
    }
}

/**
 * Answer with a status and stop.
 *
 * The route exits, which is what a route should do. A harness that includes this
 * file (defining SYNC_LIBRARY first, see the guard at the end) gets the status
 * thrown instead, the same way merge.php's MergeRefused becomes a status: what
 * the answer would be is data, and only the outermost caller ends the process.
 *
 * @return never
 */
function refuse(int $code, string $message): void
{
    if (defined('SYNC_LIBRARY')) {
        throw new SyncRefused($code, $message);
    }
    http_response_code($code);
    echo sync_respond(array('error' => $message));
    exit;
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
 * The databases the route exports, exactly as the config names them.
 *
 * A label is what a caller sees. The physical database name never travels in a
 * query string, so a caller cannot ask for a database it was not given.
 *
 * @return array<string,array{0:string,1:mysqli|null}>
 */
function configured_databases(): array
{
    $databases = array();
    foreach (db_labels() as $label => $name) {
        $databases[$label] = array($name, db_named($label));
    }

    return $databases;
}

/**
 * The column a table is paged by: the one column the catalogue marks as its key.
 *
 * A cursor has to be unique and ordered, and a key is the only column a database
 * promises that for. The query asks for COLUMN_KEY = 'PRI' rather than for a
 * declared PRIMARY KEY, because MySQL and MariaDB report a UNIQUE index as PRI
 * when it cannot contain NULL and the table has no PRIMARY KEY ("A UNIQUE index
 * may be displayed as PRI ...", INFORMATION_SCHEMA.COLUMNS): that column is
 * unique and never null, which is a cursor in every way that matters. A nullable
 * UNIQUE column stays UNI and is not chosen, because `WHERE col > 'x'` never
 * returns a NULL and paging by it would drop those rows without saying so.
 *
 * Null means the table has neither, so it can only be read one page up to the cap
 * -- which the reply says with `paged: false` instead of pretending the table was
 * read whole.
 */
function cursor_column(mysqli $link, string $database, string $table): ?string
{
    $statement = mysqli_prepare(
        $link,
        'SELECT COLUMN_NAME FROM information_schema.COLUMNS'
        . " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_KEY = 'PRI'"
        . ' ORDER BY ORDINAL_POSITION'
    );
    mysqli_stmt_bind_param($statement, 'ss', $database, $table);
    mysqli_stmt_execute($statement);
    $result = mysqli_stmt_get_result($statement);
    $keys = array();
    while ($row = mysqli_fetch_row($result)) {
        $keys[] = (string) $row[0];
    }
    mysqli_stmt_close($statement);

    if (count($keys) === 1) {
        return $keys[0];
    }

    return in_array('id', column_names($link, $database, $table), true) ? 'id' : null;
}

/**
 * Phase one: what there is to export.
 *
 * Every configured database, every table in it, its columns and the column a page
 * of it is ordered by. No rows, so this reply is small however large the databases
 * are, and a caller can ask for one table at a time from what it returns.
 *
 * @param array<string,array{0:string,1:mysqli|null}>|null $databases label =>
 *        [physical name, connection], the way export_page() takes them
 * @return array<string,mixed>
 */
function export_tables(?array $databases = null): array
{
    $databases = $databases === null ? configured_databases() : $databases;
    $out = array('action' => 'tables', 'generated_at' => gmdate('c'), 'databases' => array());

    foreach ($databases as $label => $entry) {
        [$name, $link] = $entry;
        $database = array('database' => $label, 'on' => 0, 'error' => 'not reachable', 'tables' => array());
        if ($link === null) {
            $out['databases'][] = $database;
            continue;
        }

        try {
            $names = table_names($link, $name);
        } catch (Throwable $exc) {
            $database['error'] = 'connected, tables not readable';
            $out['databases'][] = $database;
            continue;
        }

        $database['on'] = 1;
        $database['error'] = null;
        foreach ($names as $table) {
            // The merged table is this route's own output. Exporting it would
            // make every run merge its own previous merge, so it is skipped.
            if ($label === DB_PRIMARY && $table === 'merged_rows') {
                continue;
            }
            $database['tables'][] = array(
                'table' => $table,
                'columns' => column_names($link, $name, $table),
                'cursor' => cursor_column($link, $name, $table),
            );
        }
        $out['databases'][] = $database;
    }

    return $out;
}

/**
 * Phase two: one page of one table, ordered by its cursor column.
 *
 * `after` is the last key the previous page returned, so the cap cannot lose a
 * row: the walk resumes where it stopped instead of starting again from the top.
 * `more` and `next` say the same thing -- another page exists, and the value to
 * ask for it with -- so a caller that wants the whole table loops until `next` is
 * null, one bounded request at a time.
 *
 * A table with no cursor column cannot be walked that way: it is read once, up to
 * the cap, and reports `paged: false`, which is the one case where a caller still
 * has to be told there is more rather than being handed the rest.
 *
 * @param array<string,array{0:string,1:mysqli|null}>|null $databases label =>
 *        [physical name, connection]. The route passes nothing and gets what the
 *        config says; a probe passes its own, which is the only way this runs with
 *        no host and no MySQL
 * @return array<string,mixed>
 */
function export_page(string $label, string $table, ?string $after, int $cap, ?array $databases = null): array
{
    $databases = $databases === null ? configured_databases() : $databases;
    if (!isset($databases[$label])) {
        refuse(404, 'no database by that name in this configuration');
    }

    [$name, $link] = $databases[$label];
    if ($link === null) {
        refuse(503, 'that database is not reachable');
    }

    // Names come from information_schema, never from the query string: `table`
    // is matched against what the database says it has, so it cannot carry SQL.
    $columns = column_names($link, $name, $table);
    if ($columns === array()) {
        refuse(404, 'no table by that name in that database');
    }
    $cursor = cursor_column($link, $name, $table);

    $sql = 'SELECT * FROM ' . quote_identifier($table);
    if ($cursor !== null) {
        if ($after !== null && $after !== '') {
            $sql .= ' WHERE ' . quote_identifier($cursor)
                . " > '" . mysqli_real_escape_string($link, $after) . "'";
        }
        $sql .= ' ORDER BY ' . quote_identifier($cursor) . ' ASC';
    }
    $sql .= ' LIMIT ' . ($cap + 1);

    $rows = array();
    $next = null;
    $more = false;
    $result = mysqli_query($link, $sql);
    if ($result === false) {
        refuse(503, 'that table is not readable');
    }
    while ($assoc = mysqli_fetch_assoc($result)) {
        if (count($rows) >= $cap) {
            $more = true;
            break;
        }
        $values = array();
        foreach ($assoc as $column => $value) {
            $values[(string) $column] = encode_value($value);
        }
        $json = json_encode($values, JSON_UNESCAPED_SLASHES);
        $rows[] = array(
            'key' => row_key($table, $values),
            'bytes' => $json === false ? 0 : strlen($json),
            'values' => $values,
        );
        $next = $cursor === null ? null : (string) ($assoc[$cursor] ?? '');
    }
    mysqli_free_result($result);

    if ($cursor === null) {
        // No cursor, so `more` can be true with nothing to continue from: the page
        // says the table is bigger than this rather than pretending it was all of
        // it. `next` stays null, because there is no key to ask with.
        $next = null;
    }

    return array(
        'action' => 'export',
        'paged' => $cursor !== null,
        'database' => $label,
        'table' => $table,
        'columns' => $columns,
        'cursor' => $cursor,
        'after' => $after,
        'next' => $more ? $next : null,
        'more' => $more,
        'cap' => $cap,
        'rows' => $rows,
        'generated_at' => gmdate('c'),
    );
}


/**
 * The real writer: one prepared statement for the whole merge, and the
 * transaction the route wraps around it.
 *
 * merge.php decides what a row is; this only moves the bytes. That split is why
 * the rules can be executed in a test with no MySQL and no host.
 */
final class MysqliMergeWriter implements MergeWriter
{
    private mysqli $link;
    private ?mysqli_stmt $statement = null;

    public function __construct(mysqli $link)
    {
        $this->link = $link;
    }

    public function begin(): void
    {
        mysqli_begin_transaction($this->link);
    }

    /** @param array<int,mixed> $params */
    public function insert(string $sql, string $types, array $params): void
    {
        if ($this->statement === null) {
            $this->statement = mysqli_prepare($this->link, $sql);
        }
        mysqli_stmt_bind_param($this->statement, $types, ...$params);
        mysqli_stmt_execute($this->statement);
    }

    public function commit(): void
    {
        $this->close();
        mysqli_commit($this->link);
    }

    public function rollback(): void
    {
        $this->close();
        mysqli_rollback($this->link);
    }

    private function close(): void
    {
        if ($this->statement !== null) {
            mysqli_stmt_close($this->statement);
            $this->statement = null;
        }
    }
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
 * @param mysqli|null $link the route passes nothing and gets the configured
 *                          connection; a test passes a stub, which is the only
 *                          way this runs with no MySQL and no host
 * @return array<string,mixed>
 */
function merge_rows(array $rows, ?string $run = null, ?mysqli $link = null): array
{
    $link = $link ?? db();
    if ($link === null) {
        refuse(503, 'database not reachable');
    }

    try {
        ensure_merged_table($link);
    } catch (Throwable $exc) {
        refuse(503, 'could not prepare the merged_rows table');
    }

    $writer = new MysqliMergeWriter($link);
    $writer->begin();
    try {
        $result = merge_write($rows, $run, $writer, DB_PRIMARY);
        $writer->commit();
    } catch (MergeRefused $refused) {
        $writer->rollback();
        refuse($refused->status, $refused->getMessage());
    } catch (Throwable $exc) {
        $writer->rollback();
        // Never the exception text: it can name tables and, on some drivers, the
        // connection. The caller gets a status, not a schema.
        refuse(500, 'the merge was refused by the database');
    }

    return array(
        'action' => 'merge',
        'received' => count($rows),
        'accepted' => $result['accepted'],
        'distinct' => count($result['keys']),
        'on' => 1,
        'run_token' => $run,
        'merged' => merged_status($run, $link),
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
            $grants = array();
            while ($row = mysqli_fetch_row($result)) {
                $grants[] = (string) $row[0];
            }
            mysqli_free_result($result);

            // Parsed by the pure function, so the same code is exercised by the
            // local harness against real MariaDB lines instead of a copy of it.
            $objects = grant_objects($grants);
            $global = isset($objects['*.*']) ? $objects['*.*'] : array();
            $scoped = isset($objects[$name . '.*']) ? $objects[$name . '.*'] : array();
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

        // What the merge needs before it can add the run_token column: ALTER on
        // merged_rows, and CREATE if the table is not there yet. Reported here so
        // a missing grant is named in advance, rather than surfacing as the
        // merge's generic 503 after every row has been built and sent.
        if ($label === DB_PRIMARY) {
            $exists = null;
            $hasRunToken = null;
            try {
                $result = mysqli_query($link, "SHOW TABLES LIKE 'merged_rows'");
                if ($result !== false) {
                    $exists = mysqli_num_rows($result) > 0;
                    mysqli_free_result($result);
                }
                if ($exists === true) {
                    $result = mysqli_query($link, "SHOW COLUMNS FROM merged_rows LIKE 'run_token'");
                    if ($result !== false) {
                        $hasRunToken = mysqli_num_rows($result) > 0;
                        mysqli_free_result($result);
                    }
                }
            } catch (Throwable $exc) {
                $exists = null;
                $hasRunToken = null;
            }

            $onTable = privileges_on($objects, $name, 'merged_rows');
            $entry['merged_table'] = array(
                'database' => $name,
                'table' => 'merged_rows',
                'exists' => $exists,
                'has_run_token' => $hasRunToken,
                'needs_alter' => $exists === true && $hasRunToken === false,
                'privileges' => array(
                    'alter' => (bool) $onTable['alter'],
                    'drop' => (bool) $onTable['drop'],
                    'create' => (bool) $onTable['create'],
                    'insert' => (bool) $onTable['insert'],
                    'select' => (bool) $onTable['select'],
                ),
                // Nothing left to do, or permission to do it.
                'ready' => $exists === null ? null
                    : ($exists === false ? (bool) $onTable['create']
                        : ($hasRunToken === true ? true : (bool) $onTable['alter'])),
            );
        }

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

/**
 * Answer one request: the guard, then the action, then its payload.
 *
 * A function of what the request says rather than of PHP's superglobals, so the
 * refusals -- no key installed, a wrong key, a GET where a merge would write, an
 * empty or malformed body, more rows than one request may carry, an unknown
 * action -- can be executed with no host and no request. The entry point at the
 * bottom builds $request from the superglobals and prints what this returns.
 *
 * A refusal goes through refuse(), so under SYNC_LIBRARY it arrives as a
 * SyncRefused carrying the status the route would have answered with.
 *
 * @param array{method?:string,query?:array<string,mixed>,header_key?:?string,body?:string} $request
 * @param string|null $key_file where the installed key lives; null is php/import.key
 * @param mysqli|null $link the merge's connection, when the caller has one
 * @return array<string,mixed> the payload the route would serialise
 */
function sync_route(array $request, ?string $key_file = null, ?mysqli $link = null): array
{
    $key = sync_key($key_file ?? __DIR__ . '/import.key');
    if ($key === null) {
        refuse(503, 'sync disabled: no import.key on the host');
    }

    $query = isset($request['query']) && is_array($request['query']) ? $request['query'] : array();

    $given = '';
    if (isset($request['header_key'])) {
        $given = (string) $request['header_key'];
    } elseif (isset($query['key'])) {
        $given = (string) $query['key'];
    }
    if (!hash_equals($key, $given)) {
        refuse(403, 'forbidden');
    }

    $action = isset($query['action']) ? strtolower((string) $query['action']) : 'export';

    if ($action === 'status') {
        $run = isset($query['run']) ? (string) $query['run'] : null;
        if ($run !== null && preg_match('/^[A-Za-z0-9_-]{8,64}$/', $run) !== 1) {
            refuse(400, 'run token is not a plain token');
        }

        return array('action' => 'status', 'merged' => merged_status($run, $link));
    }

    if ($action === 'report') {
        return database_report();
    }

    if ($action === 'tables') {
        return export_tables();
    }

    if ($action === 'export') {
        $cap = isset($query['limit']) ? (int) $query['limit'] : SYNC_PAGE_ROWS;
        $named = isset($query['table']) ? trim((string) $query['table']) : '';
        if ($named === '') {
            // Nothing is read until a table is named: the reply that used to hold
            // every row of every table is the one that ran out of memory.
            refuse(400, 'export is one table at a time: ?action=tables lists them, '
                . 'then &table=<database>.<table>');
        }
        $parts = explode('.', $named, 2);
        if (count($parts) !== 2 || $parts[0] === '' || $parts[1] === '') {
            refuse(400, 'name the table as <database>.<table>, the way ?action=tables reports it');
        }
        $after = isset($query['after']) && (string) $query['after'] !== ''
            ? (string) $query['after']
            : null;

        return export_page($parts[0], $parts[1], $after, max(1, min(SYNC_PAGE_MAX, $cap)));
    }

    if ($action === 'merge') {
        if ((string) ($request['method'] ?? 'GET') !== 'POST') {
            refuse(405, 'merge is a POST: a GET that could write is a link that could write');
        }

        $body = isset($request['body']) ? (string) $request['body'] : '';
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

        return merge_rows($payload['rows'], $run, $link);
    }

    refuse(400, 'unknown action; use tables, export, merge, report or status');
}

// ---------------------------------------------------------------------------
// The request. Everything above is functions, so a harness can include this
// file and never dispatch: SYNC_LIBRARY says "I am the caller", refuse() then
// throws, and the include returns here.
if (defined('SYNC_LIBRARY')) {
    return;
}

/**
 * The bytes the route writes: one JSON document per request.
 *
 * An export is compact; everything else is pretty-printed. The export is a row
 * dump that only sync_database.py reads, and its whitespace is not free: measured
 * on export-shaped payloads, pretty is 58-74% more bytes (31.0 MB against 17.8 MB
 * at 40,000 rows) and peaks at 142 MB against 104 MB, and at 80,000 rows it runs
 * past PHP's 256 MB limit where compact still finishes at 208 MB. A refusal, a
 * report or a status is read by a person, so it stays indented. Slashes are left
 * alone in both, so a URL in a row is not escaped back at whoever asked.
 *
 * The per-row `bytes` count is the length of that row's own compact encoding,
 * computed in export_page() and untouched here: nothing measures this envelope.
 */
function sync_respond(array $payload): string
{
    $flags = ($payload['action'] ?? '') === 'export'
        ? JSON_UNESCAPED_SLASHES
        : JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES;

    return json_encode($payload, $flags) . "\n";
}

$request = array(
    'method' => isset($_SERVER['REQUEST_METHOD']) ? (string) $_SERVER['REQUEST_METHOD'] : 'GET',
    'query' => isset($_GET) && is_array($_GET) ? $_GET : array(),
    'header_key' => isset($_SERVER['HTTP_X_IMPORT_KEY']) ? (string) $_SERVER['HTTP_X_IMPORT_KEY'] : null,
    // Only a merge reads a body; reading php://input for anything else would
    // consume it to no purpose.
    'body' => strtolower((string) ($_GET['action'] ?? '')) === 'merge'
        ? (string) file_get_contents('php://input')
        : '',
);

echo sync_respond(sync_route($request));
