<?php
declare(strict_types=1);

/**
 * The merge decision, without the route around it.
 *
 * db_sync.php is a route: including it executes it, so nothing could ever run the
 * merge -- its SQL and per-row rules were only ever compared as text. What
 * decides something lives here instead: the one statement, its bind types, the
 * validation, and the run token. The connection, the transaction and the HTTP
 * status stay in db_sync.php, which is the thin part that needs a host.
 *
 * A refusal is thrown, not printed and exited: the route turns it back into a
 * status, and a test can assert the status without losing the process.
 *
 * Requires keys.php only. No database, no host, no route.
 */

require_once __DIR__ . '/keys.php';

/** A row this side refuses; the status is what the route should answer with. */
final class MergeRefused extends RuntimeException
{
    public int $status;

    public function __construct(int $status, string $message)
    {
        parent::__construct($message);
        $this->status = $status;
    }
}

/** Everything the merge needs from a database, and nothing else. */
interface MergeWriter
{
    public function begin(): void;

    /** @param array<int,mixed> $params */
    public function insert(string $sql, string $types, array $params): void;

    public function commit(): void;

    public function rollback(): void;
}

/** Bind types, in order: three strings, two ints, two strings. */
const MERGE_BIND_TYPES = 'sssiiss';

/**
 * The one statement every merged row goes through.
 *
 * ON DUPLICATE KEY UPDATE is why the merge is repeatable: the same key updates
 * last_seen and the run token instead of inserting a second copy, and seen_in
 * only ever grows.
 */
function merge_insert_sql(): string
{
    return 'INSERT INTO merged_rows'
        . ' (key_sha256, source_table, source_db, seen_in, row_bytes, row_json, first_seen, last_seen, run_token)'
        . ' VALUES (?, ?, ?, ?, ?, ?, UTC_TIMESTAMP(), UTC_TIMESTAMP(), ?)'
        . ' ON DUPLICATE KEY UPDATE'
        . ' seen_in = GREATEST(seen_in, VALUES(seen_in)),'
        . ' row_bytes = VALUES(row_bytes),'
        . ' row_json = VALUES(row_json),'
        . ' source_db = VALUES(source_db),'
        . ' run_token = VALUES(run_token),'
        . ' last_seen = UTC_TIMESTAMP()';
}

/**
 * Check every row and hand it to the writer.
 *
 * The key is computed here from the table name and the values, never taken from
 * the caller: a row that arrives under a key that does not describe it is
 * refused, and so is a row whose table or database label is not a plain
 * identifier.
 *
 * @param array<int,mixed> $rows
 * @param string $default_db label used when a row does not name its database
 * @return array{accepted:int, keys:array<string,bool>}
 */
function merge_write(array $rows, ?string $run, MergeWriter $writer, string $default_db): array
{
    $sql = merge_insert_sql();
    $accepted = 0;
    $keys = array();

    foreach ($rows as $row) {
        if (!is_array($row)) {
            throw new MergeRefused(400, 'every row must be an object');
        }
        $table = isset($row['table']) ? (string) $row['table'] : '';
        if (preg_match('/^[A-Za-z0-9_]{1,64}$/', $table) !== 1) {
            throw new MergeRefused(400, 'row table name is not a plain identifier');
        }
        $values = isset($row['values']) && is_array($row['values']) ? $row['values'] : array();
        if ($values === array()) {
            throw new MergeRefused(400, 'a row without values describes nothing');
        }
        $database = isset($row['source_db']) ? (string) $row['source_db'] : $default_db;
        if (preg_match('/^[A-Za-z0-9_]{1,64}$/', $database) !== 1) {
            throw new MergeRefused(400, 'row source_db is not a configured label');
        }

        $key = row_key($table, $values);
        if (isset($row['key']) && !hash_equals($key, (string) $row['key'])) {
            throw new MergeRefused(422, 'a row arrived under a key that does not describe it');
        }

        $json = json_encode($values, JSON_UNESCAPED_SLASHES);
        if ($json === false) {
            throw new MergeRefused(422, 'a row could not be encoded');
        }

        $seen = isset($row['seen']) ? max(1, min(1000, (int) $row['seen'])) : 1;
        $bytes = strlen($json);

        $writer->insert($sql, MERGE_BIND_TYPES,
                        array($key, $table, $database, $seen, $bytes, $json, $run));

        $accepted++;
        $keys[$key] = true;
    }

    return array('accepted' => $accepted, 'keys' => $keys);
}
