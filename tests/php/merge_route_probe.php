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

/** A result set the stub answers with: rows, in the shape each query asked for. */
final class StubResult
{
    /** @param array<int,array<string,mixed>> $rows */
    public function __construct(public array $rows)
    {
    }
}

/** A prepared statement: the SQL, the bind types and the values bound to it. */
final class mysqli_stmt
{
    public string $sql;
    public string $types = '';
    /** @var array<int,mixed> */
    public array $params = array();
    /** @var array<int,string> */
    public array $param_types = array();
    /** @var array<int,string> */
    public array $disagreements = array();

    public function __construct(string $sql)
    {
        $this->sql = $sql;
    }
}

/** Splits a VALUES list on commas that are not inside parentheses. */
function stub_split(string $list): array
{
    $parts = array();
    $depth = 0;
    $current = '';
    for ($index = 0; $index < strlen($list); $index++) {
        $char = $list[$index];
        if ($char === '(') {
            $depth++;
        } elseif ($char === ')') {
            $depth--;
        }
        if ($char === ',' && $depth === 0) {
            $parts[] = trim($current);
            $current = '';
            continue;
        }
        $current .= $char;
    }
    $parts[] = trim($current);

    return $parts;
}

/**
 * The statement taken apart: the columns it names, the values it writes, and
 * where a value is a placeholder rather than a literal.
 *
 * @return array{columns:array<int,string>,values:array<int,string>,placeholders:int}
 */
function stub_shape(string $sql): array
{
    $shape = array('columns' => array(), 'values' => array(), 'placeholders' => 0);
    if (preg_match('/INSERT INTO\s+\w+\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)\s*ON DUPLICATE/s', $sql, $match) !== 1) {
        return $shape;
    }
    $shape['columns'] = array_map('trim', explode(',', $match[1]));
    $shape['values'] = stub_split($match[2]);
    $shape['placeholders'] = substr_count(end($shape['values']) === '' ? '' : implode(',', $shape['values']), '?');

    return $shape;
}

/**
 * The stub connection: what the route would have sent, and what a server would
 * have ended up holding.
 */
class mysqli
{
    /** @var array<string,array<string,mixed>> committed rows, by key */
    private array $rows = array();
    /** @var array<string,array<string,mixed>> the open transaction's rows */
    private array $staged = array();

    /** @var array<int,string> every statement prepared, in order */
    public array $prepared = array();
    /** @var array<int,string> every plain query, in order */
    public array $queries = array();
    /** @var array<int,array<string,mixed>> one entry per bound+executed statement */
    public array $binds = array();
    /** @var array<int,string> placeholders that do not agree with the bind list */
    public array $disagreements = array();

    public bool $began = false;
    public bool $committed = false;
    public bool $rolledBack = false;
    /** When set, the table's columns cannot be read, which is the route's 503 path. */
    public bool $failColumns = false;
    private int $writes = 0;

    /** @return array<string,array<string,mixed>> */
    public function stored(): array
    {
        return $this->rows;
    }

    public function begin(): void
    {
        $this->began = true;
        $this->staged = $this->rows;
    }

    public function commit(): void
    {
        $this->committed = true;
        $this->rows = $this->staged;
    }

    public function rollback(): void
    {
        $this->rolledBack = true;
        $this->staged = $this->rows;
    }

    /**
     * One row through the statement: the upsert the SQL names, modelled.
     *
     * @param array<int,mixed> $params
     */
    public function apply(array $params): void
    {
        [$key, $table, $database, $seen, $bytes, $json, $run] = $params;
        $previous = $this->staged[$key] ?? null;
        $this->staged[$key] = array(
            'source_table' => $table,
            'source_db' => $database,
            // GREATEST(seen_in, VALUES(seen_in)): never the later, smaller count.
            'seen_in' => $previous === null ? $seen : max((int) $previous['seen_in'], (int) $seen),
            'row_bytes' => $bytes,
            'row_json' => $json,
            'run_token' => $run,
            // first_seen is not in the UPDATE list, so a repeat merge keeps it.
            'first_written' => $previous === null ? ++$this->writes : $previous['first_written'],
            'last_written' => ++$this->writes,
        );
    }

    /** The rows a statement's WHERE names, or all of them when it names none. */
    private function matching(string $sql): array
    {
        if (preg_match("/WHERE run_token = '([^']*)'/", $sql, $match) === 1) {
            return array_filter(
                $this->rows,
                static function (array $row) use ($match): bool {
                    return (string) $row['run_token'] === $match[1];
                }
            );
        }

        return $this->rows;
    }

    /** Answer one plain query from the rows the way the route's summaries ask. */
    public function run(string $sql): StubResult|bool
    {
        $this->queries[] = $sql;

        if (str_starts_with($sql, 'CREATE TABLE')) {
            return true;
        }
        if (str_starts_with($sql, 'ALTER TABLE')) {
            return true;
        }
        if (str_starts_with($sql, 'SHOW COLUMNS')) {
            if ($this->failColumns) {
                return false;
            }
            // The table already has run_token, so no ALTER is attempted.
            return new StubResult(array(array('Field' => 'run_token')));
        }

        $rows = $this->matching($sql);

        if (str_contains($sql, 'GROUP BY')) {
            $groups = array();
            foreach ($rows as $row) {
                $name = $row['source_db'] . "\0" . $row['source_table'];
                if (!isset($groups[$name])) {
                    $groups[$name] = array(
                        'source_db' => $row['source_db'],
                        'source_table' => $row['source_table'],
                        'rows_total' => 0,
                    );
                }
                $groups[$name]['rows_total']++;
            }
            ksort($groups);

            return new StubResult(array_values($groups));
        }

        if (str_contains($sql, 'key_sha256 FROM')) {
            $keys = array_keys($rows);
            sort($keys);
            $out = array();
            foreach ($keys as $key) {
                $out[] = array('key_sha256' => $key);
            }

            return new StubResult($out);
        }

        if (str_starts_with($sql, 'SELECT COUNT(*)')) {
            $bytes = 0;
            $last = null;
            foreach ($rows as $row) {
                $bytes += (int) $row['row_bytes'];
                $last = $last === null ? (int) $row['last_written'] : max($last, (int) $row['last_written']);
            }

            return new StubResult(array(array(
                'rows_total' => count($rows),
                'bytes_total' => $bytes,
                'last_seen' => $last === null ? null : 'stamp-' . $last,
            )));
        }

        return new StubResult(array());
    }
}

function mysqli_query($link, string $sql): StubResult|bool
{
    return $link->run($sql);
}

function mysqli_real_escape_string($link, string $value): string
{
    return $value;
}

function mysqli_num_rows($result): int
{
    return $result instanceof StubResult ? count($result->rows) : 0;
}

function mysqli_fetch_assoc($result): array|false|null
{
    if (!$result instanceof StubResult) {
        return false;
    }
    $row = array_shift($result->rows);

    return $row ?? false;
}

function mysqli_fetch_row($result): array|false|null
{
    $row = mysqli_fetch_assoc($result);

    return is_array($row) ? array_values($row) : $row;
}

function mysqli_free_result($result): bool
{
    return true;
}

function mysqli_begin_transaction($link): bool
{
    $link->begin();

    return true;
}

function mysqli_commit($link): bool
{
    $link->commit();

    return true;
}

function mysqli_rollback($link): bool
{
    $link->rollback();

    return true;
}

function mysqli_prepare($link, string $sql): mysqli_stmt
{
    $link->prepared[] = $sql;

    return new mysqli_stmt($sql);
}

/** @param mixed ...$params */
function mysqli_stmt_bind_param(mysqli_stmt $statement, string $types, ...$params): bool
{
    $statement->types = $types;
    $statement->params = $params;
    $statement->param_types = array_map(
        static function ($value): string {
            return get_debug_type($value);
        },
        $params
    );

    // The agreement this test exists for: one placeholder per bind type, one
    // bind type per value, and a type letter that describes the value it got.
    $placeholders = substr_count($statement->sql, '?');
    if ($placeholders !== strlen($types)) {
        $statement->disagreements[] = sprintf(
            '%d placeholders, %d bind types (%s)',
            $placeholders,
            strlen($types),
            $types
        );
    }
    if (strlen($types) !== count($params)) {
        $statement->disagreements[] = sprintf(
            '%d bind types, %d values',
            strlen($types),
            count($params)
        );
    }
    foreach ($params as $index => $value) {
        $letter = $types[$index] ?? '';
        $wanted = array('s' => 'string', 'i' => 'int', 'd' => 'float', 'b' => 'blob');
        $actual = get_debug_type($value);
        if (!isset($wanted[$letter]) || $wanted[$letter] !== $actual) {
            $statement->disagreements[] = sprintf(
                'value %d is %s, bound as %s',
                $index,
                $actual,
                $letter
            );
        }
    }

    return true;
}

function mysqli_stmt_execute(mysqli_stmt $statement): bool
{
    $link = $GLOBALS['stub_link'] ?? null;
    if (!$link instanceof mysqli) {
        return false;
    }
    $link->binds[] = array(
        'sql' => $statement->sql,
        'placeholders' => substr_count($statement->sql, '?'),
        'types' => $statement->types,
        'params' => $statement->params,
        'param_types' => $statement->param_types,
        'disagreements' => $statement->disagreements,
    );
    foreach ($statement->disagreements as $problem) {
        $link->disagreements[] = 'statement: ' . $problem;
    }
    $link->apply($statement->params);

    return true;
}

function mysqli_stmt_close(mysqli_stmt $statement): bool
{
    return true;
}

/** One merge, the way the route runs it: a connection, a token, a result. */
function probe_case(array $rows, ?string $run, ?mysqli $link = null): array
{
    $link = $link ?? new mysqli();
    $GLOBALS['stub_link'] = $link;

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
