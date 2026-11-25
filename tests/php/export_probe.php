<?php
declare(strict_types=1);

/**
 * Run the two-phase export for real, against a stub mysqli.
 *
 * Phase one says what there is: database, table, columns, and the column a page is
 * ordered by. Phase two returns one page of one table from a cursor, so a table
 * larger than a page is read completely by asking again rather than truncated. The
 * export is also the one place a value can be silently mangled -- mysqli hands back
 * strings whatever the column type, so a BLOB or a latin1 column is not valid UTF-8
 * and json_encode would replace the bytes it cannot read -- so encode_value()'s
 * base64 wrapper is driven here too.
 *
 * The stub answers information_schema and applies the cursor's WHERE, ORDER BY and
 * LIMIT to the rows a fixture registered, so the walk here is three requests deep
 * and the SQL is the real one.
 *
 * Fixtures are shaped like a real host: the primary label is `toolkit`, its table
 * is `merged_rows`, a neighbour that happens to have a table of the same name is a
 * different database, and one database is unreachable while another's SELECT is
 * refused.
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
 * encode_value(), with what a reader needs to judge it: the encoded form, its
 * JSON, and whether the bytes survive.
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
 * A connection that reports one table, its primary key, and answers its rows.
 *
 * @param array<int,array<string,mixed>> $rows
 * @param array<int,string> $columns
 * @param array<int,string> $keys
 */
function with_table(string $table, array $rows, array $columns = array('id'), array $keys = array('id')): mysqli
{
    $link = new mysqli();
    if ($keys !== array()) {
        // Registered first: the first needle that matches answers, and the cursor
        // query asks for COLUMN_KEY = 'PRI' before it asks for every column.
        $link->answer(
            "COLUMN_KEY = 'PRI'",
            array_map(static fn (string $name) => array('COLUMN_NAME' => $name), $keys)
        );
    }
    $link->answer('FROM information_schema.TABLES', array(array('TABLE_NAME' => $table)));
    $link->answer(
        'FROM information_schema.COLUMNS',
        array_map(static fn (string $name) => array('COLUMN_NAME' => $name), $columns)
    );
    $link->answer('FROM `' . $table . '`', $rows);

    return $link;
}

/** What a page refused with, as data instead of an exit. */
function attempt(callable $work): array
{
    try {
        return array('page' => $work());
    } catch (SyncRefused $refused) {
        return array('status' => $refused->status, 'message' => $refused->getMessage());
    }
}

$journal = with_table('broadcast_journal', array(
    array('id' => '1', 'hash' => '0xaa'),
    array('id' => '2', 'hash' => '0xbb'),
    array('id' => '3', 'hash' => '0xcc'),
), array('id', 'hash'), array('id'));

// No primary key and no `id`: this table can only be read one page up to the cap,
// which is why one of its pages below is asked for with a cap it cannot fit.
$binary = with_table('cjd_source', array(
    array('filename' => 'cjd.php', 'content' => "latin1 \xB1 byte"),
    array('filename' => 'notes.txt', 'content' => 'plain text'),
    array('filename' => 'report.json', 'content' => '{"ok":true}'),
), array('filename', 'content'), array());

$primary = with_table('merged_rows', array(array('id' => '1', 'key_sha256' => 'a')));
$neighbour = with_table('merged_rows', array(array('id' => '1')), array('id'), array('id'));

$locked = with_table('orders', array(array('id' => '1')));
$locked->refuse('FROM `orders`');

// A database that reports a table it then has no columns for: the shape a
// half-created table or a wrong name has.
$sparse = new mysqli();
$sparse->answer('FROM information_schema.TABLES', array(array('TABLE_NAME' => 'present')));
$sparse->answer("COLUMN_KEY = 'PRI'", array());
$sparse->answer('FROM information_schema.COLUMNS', array());

$databases = array(
    'toolkit' => array('toolkit', $primary),
    'journal' => array('probe_toolkit', $journal),
    'cjd' => array('cjd_toolkit', $binary),
    'neighbour' => array('lovewebconnection', $neighbour),
    'offline' => array('offline_db', null),
    'locked' => array('probe_orders', $locked),
    'sparse' => array('probe_sparse', $sparse),
);

$listing = export_tables($databases);

// The walk: two pages of two, then the cursor runs out.
$first = export_page('journal', 'broadcast_journal', null, 2, $databases);
$second = export_page('journal', 'broadcast_journal', $first['next'], 2, $databases);
// Past the last key: the walk has ended, and asking again from where it stopped
// is what returns nothing rather than page one over again.
$third = export_page('journal', 'broadcast_journal', '3', 2, $databases);

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
    'listing' => $listing,
    'pages' => array('first' => $first, 'second' => $second, 'third' => $third),
    // The bytes of the binary table, which has no cursor: one page, and it says so.
    'single' => export_page('cjd', 'cjd_source', null, 50, $databases),
    // The same table with a cap it does not fit: no cursor to continue from, so
    // this is the one page that can still stop a run short of the table.
    'keyless_capped' => export_page('cjd', 'cjd_source', null, 1, $databases),
    // What each request actually asked the database, so the cursor is visible as
    // SQL rather than inferred from which rows came back.
    'journal_queries' => $journal->queries,
    'refusals' => array(
        'no database' => attempt(fn () => export_page('nowhere', 't', null, 2, $databases)),
        'no table' => attempt(fn () => export_page('sparse', 'present', null, 2, $databases)),
        'offline' => attempt(fn () => export_page('offline', 't', null, 2, $databases)),
        'refused select' => attempt(fn () => export_page('locked', 'orders', null, 2, $databases)),
    ),
    // The replies as the route writes them, so a client can be run against the
    // real bytes instead of a re-encoding of the same PHP array. A page is compact
    // because it is a row dump; the other actions are read by a person.
    'replies' => array(
        'export' => sync_respond($first),
        'status' => sync_respond(array(
            'action' => 'status',
            'merged' => array('rows' => 4, 'bytes' => 900, 'digest' => str_repeat('ab', 32)),
        )),
        'error' => sync_respond(array('error' => 'forbidden')),
    ),
);

echo json_encode($report, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE), "\n";
