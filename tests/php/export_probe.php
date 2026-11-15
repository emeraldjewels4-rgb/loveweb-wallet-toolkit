<?php
declare(strict_types=1);

/**
 * Run export_all() for real, against a stub mysqli.
 *
 * The export is the read half of the sync, and the one place where a value can be
 * silently mangled: mysqli hands back strings whatever the column type, so a BLOB
 * or a latin1 column is not valid UTF-8 and json_encode would replace the bytes it
 * cannot read. encode_value() wraps those in base64 instead, and nothing had ever
 * executed it.
 *
 * The stub answers the two information_schema queries and each SELECT, so this
 * drives the real code path: the cap is applied by asking for cap + 1 rows and
 * reporting `truncated`, the merged table is skipped in the primary database only,
 * and one unreachable or unreadable database does not take the export with it.
 *
 * Fixtures are shaped like a real host: the primary label is `toolkit`, its table
 * is `merged_rows`, and a neighbour that happens to have a table of the same name
 * is a different database with a different label.
 *
 *     php -n tests/php/export_probe.php
 */

if (extension_loaded('mysqli')) {
    echo json_encode(array('unavailable' => 'the mysqli extension is loaded, so the stub cannot stand in')), "\n";
    exit(2);
}

define('SYNC_LIBRARY', true);

require_once __DIR__ . '/../../php/db_sync.php';
require_once __DIR__ . '/mysqli_stub.php';

/**
 * encode_value(), through the function the export uses, with what a reader needs
 * to judge it: the encoded form, its JSON, and whether the bytes survive.
 *
 * @param mixed $value
 * @return array<string,mixed>
 */
function encoded($value): array
{
    $out = encode_value($value);
    $wrapped = is_array($out) && isset($out['b64']) ? (string) base64_decode($out['b64'], true) : null;

    return array(
        'value' => $out,
        'json' => json_encode($out, JSON_UNESCAPED_SLASHES),
        // The bytes the export would hand back for this column, after decoding.
        'round_trip' => $wrapped === null ? null : base64_encode($wrapped),
    );
}

/**
 * A connection that reports one table and answers its rows.
 *
 * @param array<int,array<string,mixed>> $rows
 * @param array<int,string> $columns
 */
function with_table(string $table, array $rows, array $columns = array('id')): mysqli
{
    $link = new mysqli();
    $link->answer('FROM information_schema.TABLES', array(array('TABLE_NAME' => $table)));
    $link->answer(
        'FROM information_schema.COLUMNS',
        array_map(static fn (string $name) => array('COLUMN_NAME' => $name), $columns)
    );
    $link->answer('FROM `' . $table . '`', $rows);

    return $link;
}

$journal = with_table('broadcast_journal', array(
    array('id' => '1', 'hash' => '0xaa'),
    array('id' => '2', 'hash' => '0xbb'),
    array('id' => '3', 'hash' => '0xcc'),
), array('id', 'hash'));

$binary = with_table('cjd_source', array(
    array('filename' => 'cjd.php', 'content' => "latin1 \xB1 byte"),
), array('filename', 'content'));

$primary = with_table('merged_rows', array(array('id' => '1', 'key_sha256' => 'a')));
$neighbour = with_table('merged_rows', array(array('id' => '1')));

$locked = with_table('orders', array(array('id' => '1')));
$locked->refuse('FROM `orders`');

$databases = array(
    'toolkit' => array('toolkit', $primary),
    'journal' => array('fiatgodx_toolkit', $journal),
    'cjd' => array('cjd_toolkit', $binary),
    'neighbour' => array('lovewebconnection', $neighbour),
    'offline' => array('offline_db', null),
    'locked' => array('fiatgodx_orders', $locked),
);

$camped = export_all(2, $databases);
$exact = export_all(3, $databases);

$report = array(
    'encoded' => array(
        'null' => encoded(null),
        'text' => encoded('plain'),
        'multibyte' => encoded('ü €'),
        'invalid_utf8' => encoded("latin1 \xB1 byte"),
        'nul' => encoded("a\x00b"),
        'whole_float' => encoded(1.0),
        'int' => encoded(1),
    ),
    'capped' => $camped,
    'exact' => $exact,
    // What the export actually asked the database, so the cap is visible as a
    // LIMIT rather than inferred from the row count.
    'journal_queries' => $journal->queries,
    // The reply as the route writes it, so a client can be run against the real
    // bytes instead of against a re-encoding of the same PHP array.
    'envelope' => sync_respond($camped),
);

echo json_encode($report, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE), "\n";
