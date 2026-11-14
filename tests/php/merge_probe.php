<?php
declare(strict_types=1);

/**
 * Run merge_write() for real, with a writer that records instead of writes.
 *
 * db_sync.php is a route and executes on include, so merge_rows() never ran
 * anywhere but a host. merge.php holds the decisions; this drives them with a
 * stub MergeWriter and prints what happened as JSON, which
 * test_merge_rows.py asserts on.
 *
 * The stub models ON DUPLICATE KEY UPDATE with GREATEST as PHP's max(): that is
 * the merge's stated intent, not MySQL's implementation of it. What this proves
 * is that every row goes through one upsert naming those columns; that MySQL
 * evaluates GREATEST the same way is only provable against a real server.
 *
 * Not under php/: the deploy action uploads that directory, and a probe that
 * executes merges has no business on the host.
 *
 *     php tests/php/merge_probe.php
 */

require_once __DIR__ . '/../../php/merge.php';

final class StubWriter implements MergeWriter
{
    /** @var array<int,mixed> */
    public array $events = [];

    /** @var array<string,array<string,mixed>> committed rows, by key */
    public array $stored = [];

    /** @var array<string,array<string,mixed>> rows written by the open transaction */
    private array $staged = [];

    public function begin(): void
    {
        $this->events[] = 'begin';
        // A transaction sees what was committed before it, so the staged rows start
        // from the stored table rather than from nothing.
        $this->staged = $this->stored;
    }

    /** @param array<int,mixed> $params */
    public function insert(string $sql, string $types, array $params): void
    {
        $this->events[] = [
            'kind' => 'insert',
            'sql' => $sql,
            'types' => $types,
            'param_types' => array_map(
                static function ($value): string {
                    return get_debug_type($value);
                },
                $params
            ),
            'params' => $params,
        ];

        [$key, $table, $database, $seen, $bytes, $json, $run] = $params;
        $previous = $this->staged[$key] ?? null;
        $this->staged[$key] = [
            'source_table' => $table,
            'source_db' => $database,
            // GREATEST(seen_in, VALUES(seen_in)): a row seen twice keeps the
            // larger count, never the last one.
            'seen_in' => $previous === null ? $seen : max($previous['seen_in'], $seen),
            'row_bytes' => $bytes,
            'row_json' => $json,
            'run_token' => $run,
        ];
    }

    public function commit(): void
    {
        $this->events[] = 'commit';
        $this->stored = $this->staged;
        $this->staged = [];
    }

    public function rollback(): void
    {
        $this->events[] = 'rollback';
        $this->staged = [];
    }
}

/**
 * One merge, the way the route runs it: begin, write, commit or roll back.
 *
 * @param array<int,mixed> $rows
 * @return array<string,mixed>
 */
function probe_merge(array $rows, ?string $run, ?StubWriter $writer = null): array
{
    $writer = $writer ?? new StubWriter();
    $writer->begin();
    try {
        $result = merge_write($rows, $run, $writer, 'toolkit');
        $writer->commit();

        return [
            'ok' => true,
            'accepted' => $result['accepted'],
            'keys' => array_keys($result['keys']),
            'events' => $writer->events,
            'stored' => $writer->stored,
        ];
    } catch (MergeRefused $refused) {
        $writer->rollback();

        return [
            'ok' => false,
            'status' => $refused->status,
            'message' => $refused->getMessage(),
            'events' => $writer->events,
            'stored' => $writer->stored,
        ];
    }
}

$three = [
    ['table' => 'broadcast_journal', 'values' => ['id' => 1, 'hash' => '0xaa'], 'seen' => 2],
    ['table' => 'cjd_source', 'values' => ['filename' => 'cjd.php', 'bytes' => 65154]],
    ['table' => 'cjd_source', 'values' => ['ratio' => 1.0, 'note' => 'ü']],
];

$clamped = [
    ['table' => 't', 'values' => ['a' => 1], 'seen' => 0],
    ['table' => 't', 'values' => ['a' => 2], 'seen' => 9999],
    ['table' => 't', 'values' => ['a' => 3]],
];

$refusals = [
    'not an object' => ['just a string'],
    'bad table name' => [['table' => 'bad name; drop', 'values' => ['a' => 1]]],
    'no values' => [['table' => 't', 'values' => []]],
    'bad source_db' => [['table' => 't', 'values' => ['a' => 1], 'source_db' => 'no such db']],
    'key mismatch' => [['table' => 't', 'values' => ['a' => 1], 'key' => str_repeat('0', 64)]],
    'unencodable' => [['table' => 't', 'values' => ['a' => "\xB1"]]],
];

$report = [
    'three' => probe_merge($three, str_repeat('a', 32)),
    'clamped' => probe_merge($clamped, str_repeat('b', 32)),
];

// Two runs over the same row: the second must update the first, not add a copy.
$twice = new StubWriter();
$first = probe_merge([['table' => 't', 'values' => ['a' => 1], 'seen' => 3]], str_repeat('c', 32), $twice);
$second = probe_merge([['table' => 't', 'values' => ['a' => 1], 'seen' => 1]], str_repeat('d', 32), $twice);
$report['duplicate'] = ['first' => $first, 'second' => $second, 'stored' => $twice->stored];

$report['refusals'] = [];
foreach ($refusals as $name => $rows) {
    $report['refusals'][$name] = probe_merge($rows, str_repeat('e', 32));
}

echo json_encode($report, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE), "\n";
