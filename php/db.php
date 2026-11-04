<?php
declare(strict_types=1);

/**
 * Optional MySQL access, over mysqli.
 *
 * Why mysqli and not PDO: this host's PHP 8.1 build ships `mysqlnd` + `mysqli`
 * and NO PDO whatsoever (`Class "PDO" not found`, `PDO::getAvailableDrivers()`
 * unreachable). The first version of this file used PDO, so a database that was
 * correctly created and correctly granted reported itself as "configured but not
 * reachable" -- the connection never got far enough to be refused. A probe that
 * printed the real exception found it. So this file targets the driver that is
 * actually here rather than pretending to be driver-agnostic.
 *
 * The toolkit still works with no database at all: broadcast.php's file journal
 * is enough. This adds a shared one for a host that has MySQL and nothing else,
 * so a send that dies mid-batch leaves evidence somewhere durable.
 *
 * More than one database is supported, because the account has more than one and
 * burying that in a single hardcoded name is how a tool ends up unable to answer
 * a question about its own host. The primary database is where the journal
 * lives; the others are readable neighbours (config.local.php `others`), listed
 * and counted but never written to.
 *
 * Credentials live in config.local.php, which is NOT in the repository
 * (php/.gitignore) and is uploaded on its own. They are never printed:
 * db_status() and db_overview() report configured/connected/tables, never the
 * password or host, because a status page that echoes its DSN is a status page
 * that leaks one.
 *
 * A missing or broken config is not an error. db() returns null, the site still
 * renders, and the status says "not configured" -- a database that is down must
 * not take the read-only preview with it.
 */

/** Loads config.local.php: host, name, user, password, optional port and others. */
function db_config(): ?array
{
    static $loaded = false;
    static $config = null;

    if ($loaded) {
        return $config;
    }
    $loaded = true;

    $path = __DIR__ . '/config.local.php';
    if (!is_file($path)) {
        return null;
    }

    $candidate = require $path;
    if (!is_array($candidate) || empty($candidate['name']) || empty($candidate['user'])) {
        return null;
    }

    $config = $candidate;
    return $config;
}

/** The label used for the database the toolkit owns. */
const DB_PRIMARY = 'toolkit';

/**
 * Every configured database as label => physical name, primary first.
 * The labels are what the pages show; the names are what MySQL knows.
 *
 * @return array<string,string>
 */
function db_labels(): array
{
    $config = db_config();
    if ($config === null) {
        return array();
    }

    $labels = array(DB_PRIMARY => (string) $config['name']);
    if (isset($config['others']) && is_array($config['others'])) {
        foreach ($config['others'] as $label => $name) {
            $label = (string) $label;
            if ($label === '' || $label === DB_PRIMARY || !is_string($name) || $name === '') {
                continue;
            }
            $labels[$label] = $name;
        }
    }

    return $labels;
}

/**
 * A live connection to one configured database, or null.
 *
 * Only labels from db_labels() resolve: a request cannot name a database, so a
 * query string cannot reach one that was never configured.
 */
function db_named(string $label): ?mysqli
{
    static $links = array();

    if (array_key_exists($label, $links)) {
        return $links[$label];
    }
    $links[$label] = null;

    $config = db_config();
    if ($config === null || !function_exists('mysqli_connect')) {
        return null;
    }

    $labels = db_labels();
    if (!isset($labels[$label])) {
        return null;
    }

    try {
        mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT);
        $link = mysqli_connect(
            isset($config['host']) ? (string) $config['host'] : 'localhost',
            (string) $config['user'],
            isset($config['password']) ? (string) $config['password'] : '',
            $labels[$label],
            isset($config['port']) ? (int) $config['port'] : 3306
        );
        mysqli_set_charset($link, 'utf8mb4');
        $links[$label] = $link;
    } catch (Throwable $exc) {
        $links[$label] = null; // a database problem must not break the page
    }

    return $links[$label];
}

/** The primary connection, which is the only one anything writes to. */
function db(): ?mysqli
{
    return db_named(DB_PRIMARY);
}

/**
 * Whether the primary database is wired up, reachable, and has its table.
 * Contains no credentials and no host: safe to return in a public JSON response.
 */
function db_status(): array
{
    $configured = db_config() !== null;
    $status = array(
        'configured' => $configured,
        'connected' => false,
        // Binary on/off, stated rather than inferred: 01 = reachable, 00 = not.
        // "connected" is the boolean; "on" is the same fact as a digital bit so a
        // caller never has to guess which of several fields means "working".
        'on' => 0,
        'table' => false,
        'driver' => function_exists('mysqli_connect') ? 'mysqli' : 'none',
        'detail' => $configured ? 'configured' : 'not configured',
    );

    $link = db();
    if ($link === null) {
        if ($configured) {
            $status['detail'] = 'configured but not reachable';
        }
        return $status;
    }

    $status['connected'] = true;
    $status['on'] = 1;
    $status['detail'] = 'connected';

    try {
        ensure_schema($link);
        $status['table'] = true;
    } catch (Throwable $exc) {
        $status['detail'] = 'connected, schema not ready';
    }

    return $status;
}

/**
 * Every configured database with its tables and approximate row counts.
 *
 * Row counts come from information_schema.TABLES, which is a cheap metadata
 * read: an exact COUNT(*) per table would mean scanning other applications'
 * data on every page view, and that is not a thing a status page should do.
 *
 * @return array<string,array<string,mixed>>
 */
function db_overview(): array
{
    $overview = array();

    foreach (db_labels() as $label => $name) {
        $entry = array(
            'database' => $name,
            'connected' => false,
            'on' => 0,
            'table_count' => 0,
            'tables' => array(),
            'detail' => 'not reachable',
        );

        $link = db_named($label);
        if ($link === null) {
            $overview[$label] = $entry;
            continue;
        }

        $entry['connected'] = true;
        $entry['on'] = 1;
        $entry['detail'] = 'connected';

        try {
            $statement = mysqli_prepare(
                $link,
                'SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0) AS row_estimate'
                . ' FROM information_schema.TABLES WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME'
            );
            mysqli_stmt_bind_param($statement, 's', $name);
            mysqli_stmt_execute($statement);
            $result = mysqli_stmt_get_result($statement);
            while ($row = mysqli_fetch_assoc($result)) {
                $entry['tables'][] = array(
                    'name' => (string) $row['TABLE_NAME'],
                    'rows_estimate' => (int) $row['row_estimate'],
                );
            }
            mysqli_stmt_close($statement);
            $entry['table_count'] = count($entry['tables']);
        } catch (Throwable $exc) {
            // Reachable but not readable: the user has no SELECT on it.
            $entry['detail'] = 'connected, no read privilege';
        }

        $overview[$label] = $entry;
    }

    return $overview;
}

/**
 * Create the journal table if it is absent. Idempotent, so it costs one metadata
 * check per process and cannot fail a page that is otherwise fine.
 *
 * Only ever called on the primary database: the toolkit reads its neighbours,
 * it does not rearrange them.
 */
function ensure_schema(mysqli $link): void
{
    mysqli_query(
        $link,
        'CREATE TABLE IF NOT EXISTS broadcast_journal ('
        . ' id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,'
        . ' tx_hash VARCHAR(80) NOT NULL,'
        . ' status VARCHAR(20) NOT NULL,'
        . ' attempts INT NOT NULL DEFAULT 0,'
        . ' error VARCHAR(300) NOT NULL DEFAULT \'\','
        . ' recorded_at DATETIME NOT NULL,'
        . ' KEY tx_hash_idx (tx_hash)'
        . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
}

/**
 * A Journal with the same shape as broadcast.php's file journal, in MySQL.
 *
 * record() is written before a send for the same reason the file one is: an
 * interrupted run must still be able to answer "did it go?".
 */
final class DbJournal
{
    private mysqli $link;

    public function __construct(mysqli $link)
    {
        $this->link = $link;
        ensure_schema($link);
    }

    /** @param array<string,mixed> $extra accepted for signature parity with Journal */
    public function record(string $txHash, string $status, int $attempts = 0, string $error = '', array $extra = array()): void
    {
        $hash = substr($txHash, 0, 80);
        $state = substr($status, 0, 20);
        $tries = max(0, $attempts);
        $message = substr($error, 0, 300);

        $statement = mysqli_prepare(
            $this->link,
            'INSERT INTO broadcast_journal (tx_hash, status, attempts, error, recorded_at)'
            . ' VALUES (?, ?, ?, ?, UTC_TIMESTAMP())'
        );
        mysqli_stmt_bind_param($statement, 'ssis', $hash, $state, $tries, $message);
        mysqli_stmt_execute($statement);
        mysqli_stmt_close($statement);
    }

    /**
     * Newest row per hash, matching load_journal()'s contract.
     *
     * @return array<string,array<string,mixed>>
     */
    public function load(int $limit = 1000): array
    {
        $limit = max(1, min(5000, $limit));
        $result = mysqli_query(
            $this->link,
            'SELECT tx_hash, status, attempts, error, recorded_at'
            . ' FROM broadcast_journal ORDER BY id DESC LIMIT ' . $limit
        );

        $out = array();
        while ($row = mysqli_fetch_assoc($result)) {
            $hash = (string) $row['tx_hash'];
            if (!isset($out[$hash])) {
                $out[$hash] = $row; // newest first, so the first seen wins
            }
        }
        mysqli_free_result($result);

        return $out;
    }

    public function is_durable(): bool
    {
        return mysqli_query($this->link, 'SELECT 1') !== false;
    }
}
