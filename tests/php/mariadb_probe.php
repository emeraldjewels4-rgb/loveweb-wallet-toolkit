<?php
declare(strict_types=1);

/**
 * cursor_column() and the walk, against a real server instead of a stub.
 *
 * Every other probe answers `SELECT COLUMN_NAME ... AND COLUMN_KEY = 'PRI'` from
 * mysqli_stub.php, which means the one thing that decides how a table is paged --
 * how MySQL and MariaDB actually fill COLUMN_KEY for a primary key, a unique key,
 * a plain column and a table with neither -- has never been read from a catalogue
 * that was not written by us. A stub cannot be wrong about MariaDB; it can only
 * agree with our assumption about it.
 *
 * So this creates four tables with the shapes that matter and asks the route:
 *
 *   walked       single-column PRIMARY KEY            -> cursor `id`
 *   by_id        no PRIMARY KEY, an `id` column       -> cursor `id`, by fallback
 *   unique_only  UNIQUE only (COLUMN_KEY = 'UNI')     -> cursor NULL, not `id`
 *   keyless      no key and no `id`                   -> cursor NULL
 *
 * then walks `walked` two pages deep with a cap it does not fit in one, and reads
 * a keyless table once to see `paged: false` come from the server's own answer
 * rather than a fixture's.
 *
 * Destructive only inside the database it is given: the four tables are dropped
 * and recreated there. It is meant for a scratch database (the CI service creates
 * `toolkit_probe`), never for a configured host database.
 *
 *     DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=toolkit DB_PASSWORD=... DB_NAME=toolkit_probe \
 *         php tests/php/mariadb_probe.php
 *
 * Exit 0 every check passed, 1 one failed, 2 no server or no mysqli extension --
 * a CI leg treats 2 as a failure, because a leg that could not reach the server
 * verified nothing.
 */

define('SYNC_LIBRARY', true);

require_once __DIR__ . '/../../php/db_sync.php';

/** @var array<int,array{name:string,ok:bool}> */
$checks = array();

function check(bool $ok, string $name): void
{
    global $checks;
    $checks[] = array('name' => $name, 'ok' => $ok);
    echo ($ok ? '  PASS  ' : '  FAIL  '), $name, "\n";
}

/** One table's entry from the listing, or null. */
function listing_entry(array $listing, string $table): ?array
{
    foreach ($listing['databases'] as $database) {
        foreach ($database['tables'] as $entry) {
            if ($entry['table'] === $table) {
                return $entry;
            }
        }
    }

    return null;
}

/** @return array<int,string> the cursor values a page returned */
function page_keys(array $page): array
{
    return array_map(
        static fn (array $row) => (string) $row['values'][$page['cursor']],
        $page['rows']
    );
}

echo "cursor_column() against a real server\n";
echo "  section: the driver and the server\n";

if (!extension_loaded('mysqli')) {
    echo "  unavailable: the mysqli extension is not loaded (install php-mysql)\n";
    echo json_encode(array('unavailable' => 'mysqli is not loaded')), "\n";
    exit(2);
}

$host = getenv('DB_HOST') ?: '127.0.0.1';
$port = (int) (getenv('DB_PORT') ?: 3306);
$user = getenv('DB_USER') ?: 'toolkit';
$password = getenv('DB_PASSWORD');
$password = $password === false ? '' : $password;
$name = getenv('DB_NAME') ?: 'toolkit_probe';

try {
    mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT);
    $link = mysqli_connect($host, $user, $password, $name, $port);
    mysqli_set_charset($link, 'utf8mb4');
} catch (Throwable $exc) {
    // The message names host and user, never the password; nothing here prints the
    // password, and no check can pass without a connection.
    echo '  unavailable: ', $exc->getMessage(), "\n";
    echo json_encode(array('unavailable' => $exc->getMessage())), "\n";
    exit(2);
}

$server = (string) mysqli_get_server_info($link);
check(true, sprintf('connected to %s as %s on %s', $server, $user, $name));

echo "  section: the tables this server actually has\n";

foreach (array('walked', 'by_id', 'unique_only', 'keyless') as $table) {
    mysqli_query($link, 'DROP TABLE IF EXISTS `' . $table . '`');
}

mysqli_query(
    $link,
    'CREATE TABLE `walked` ('
    . ' `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,'
    . ' `label` VARCHAR(32) NOT NULL,'
    . ' PRIMARY KEY (`id`)'
    . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
);
mysqli_query($link, "INSERT INTO `walked` (`label`) VALUES ('alpha'), ('beta'), ('gamma')");

// No PRIMARY KEY, but an `id` column: the fallback cursor_column() promises.
mysqli_query(
    $link,
    'CREATE TABLE `by_id` ('
    . ' `id` BIGINT NOT NULL,'
    . ' `note` VARCHAR(32) NOT NULL'
    . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
);
mysqli_query($link, "INSERT INTO `by_id` (`id`, `note`) VALUES (10, 'a'), (11, 'b'), (12, 'c')");

// A unique key is not a primary key: COLUMN_KEY is 'UNI' here, and a cursor has
// to come from 'PRI' only. There is no `id`, so the honest answer is NULL.
mysqli_query(
    $link,
    'CREATE TABLE `unique_only` ('
    . ' `code` VARCHAR(16) NOT NULL,'
    . ' `note` VARCHAR(16) NOT NULL,'
    . ' UNIQUE KEY `code_u` (`code`)'
    . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
);
mysqli_query($link, "INSERT INTO `unique_only` (`code`, `note`) VALUES ('x', 'one'), ('y', 'two')");

mysqli_query(
    $link,
    'CREATE TABLE `keyless` ('
    . ' `slug` VARCHAR(32) NOT NULL,'
    . ' `note` VARCHAR(32) NOT NULL'
    . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
);
mysqli_query($link, "INSERT INTO `keyless` (`slug`, `note`) VALUES ('one', 'a'), ('two', 'b')");

$databases = array('toolkit' => array($name, $link));

try {
    $listing = export_tables($databases);
} catch (Throwable $exc) {
    echo '  FAIL  the listing raised ', $exc->getMessage(), "\n";
    exit(1);
}

$database = $listing['databases'][0];
check($database['database'] === 'toolkit', 'the listing names the configured label');
check($database['on'] === 1 and $database['error'] === null,
      'and says the database is on, with no error');

$orders = array();
foreach ($database['tables'] as $entry) {
    $orders[$entry['table']] = $entry;
}

check(count($orders) === 4,
      sprintf('it found the four tables it created (%d)', count($orders)));
foreach (array('walked', 'by_id', 'unique_only', 'keyless') as $table) {
    check(isset($orders[$table]), sprintf('%s is in the listing', $table));
}

echo "  section: the cursor comes from COLUMN_KEY, as the server reports it\n";

check($orders['walked']['cursor'] === 'id',
      sprintf("a primary key is the cursor (%s)", var_export($orders['walked']['cursor'], true)));
check($orders['walked']['columns'] === array('id', 'label'),
      sprintf('columns in ordinal order (%s)', implode(',', $orders['walked']['columns'])));

check($orders['by_id']['cursor'] === 'id',
      sprintf("with no primary key, `id` is the cursor (%s)", var_export($orders['by_id']['cursor'], true)));

check($orders['unique_only']['cursor'] === null,
      sprintf("a UNIQUE key alone is not a cursor (%s)", var_export($orders['unique_only']['cursor'], true)));
check($orders['keyless']['cursor'] === null,
      sprintf("nor is a table with neither a key nor an `id` (%s)", var_export($orders['keyless']['cursor'], true)));

echo "  section: two pages, walked by cursor\n";

$first = export_page('toolkit', 'walked', null, 2, $databases);
check($first['paged'] === true and $first['cursor'] === 'id',
      'the first page says it is paged, by `id`');
check(page_keys($first) === array('1', '2'),
      sprintf('and carries the first two keys in order (%s)', implode(',', page_keys($first))));
check($first['more'] === true and $first['next'] === '2',
      sprintf("with more:true and the cursor to continue from (%s)",
              var_export($first['next'], true)));

$second = export_page('toolkit', 'walked', (string) $first['next'], 2, $databases);
check(page_keys($second) === array('3'),
      sprintf('the second page resumes after it, with the rest (%s)', implode(',', page_keys($second))));
check($second['more'] === false and $second['next'] === null,
      'and says the table is exhausted, so the walk ends');

$walked = array_merge(page_keys($first), page_keys($second));
check($walked === array('1', '2', '3'),
      sprintf('both pages together are the whole table, each row once (%s)', implode(',', $walked)));

$past = export_page('toolkit', 'walked', '3', 2, $databases);
check($past['rows'] === array() and $past['more'] === false,
      'asking past the last key returns nothing, not page one again');

echo "  section: the fallback and the keyless case, on the server's answer\n";

$fallback = export_page('toolkit', 'by_id', null, 1, $databases);
check($fallback['paged'] === true and $fallback['cursor'] === 'id',
      'a table with no primary key is still paged, by `id`');
check(page_keys($fallback) === array('10') and $fallback['next'] === '10' and $fallback['more'] === true,
      sprintf('one row, and the cursor to continue from (%s)', var_export($fallback['next'], true)));

$keyless = export_page('toolkit', 'keyless', null, 1, $databases);
check($keyless['paged'] === false and $keyless['cursor'] === null,
      'a table with no cursor says paged:false');
check(count($keyless['rows']) === 1 and $keyless['more'] === true and $keyless['next'] === null,
      'and more:true with nothing to continue from, instead of claiming it read the table');

$unique = export_page('toolkit', 'unique_only', null, 1, $databases);
check($unique['paged'] === false and $unique['cursor'] === null,
      'a unique key alone leaves the table unwalkable, which is the server saying so');

echo "  section: a table that is not there\n";

$refused = null;
try {
    export_page('toolkit', 'no_such_table', null, 2, $databases);
} catch (SyncRefused $exc) {
    $refused = $exc;
}
check($refused !== null and $refused->status === 404,
      sprintf('an unknown table is a 404, from an empty catalogue row (%s)',
              $refused === null ? 'no refusal' : (string) $refused->status));

$failed = 0;
foreach ($checks as $entry) {
    $failed += $entry['ok'] ? 0 : 1;
}

printf("\n%s (%d checks, %d failed, server %s)\n",
       $failed === 0 ? 'ALL CHECKS PASSED' : 'CHECKS FAILED',
       count($checks), $failed, $server);

echo json_encode(array(
    'server' => $server,
    'extension' => 'mysqli',
    'listing' => $listing,
    'pages' => array('first' => $first, 'second' => $second, 'past' => $past),
    'keyless' => $keyless,
    'checks' => $checks,
), JSON_UNESCAPED_SLASHES), "\n";

exit($failed === 0 ? 0 : 1);
