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
 * The database the blockchain interface reports on, and the chain it is wired to.
 *
 * These are the defaults a first run seeds into `chain_connections`; from then on
 * the stored row is the truth, so changing the endpoint is an UPDATE rather than
 * a deploy. Nothing reads the constants again once the row exists.
 */
const CHAIN_LABEL = 'lovewebconnection';
const CHAIN_NETWORK = 'polygon';
const CHAIN_ID = 137;
const CHAIN_RPC = 'https://polygon-bor-rpc.publicnode.com';
const CHAIN_CURRENCY = 'POL';
const CHAIN_EXPLORER = 'https://polygonscan.com';

/** The host group every connection is surfaced under. */
const CHAIN_GATEWAY = 'web9999';

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

/**
 * The GRANT targets a set of SHOW GRANTS lines describes, normalised.
 *
 * Split out of the report so the parsing can be exercised against real MariaDB
 * output without a database: the lines are the only input, and the map returned
 * is exactly what privileges_on() consumes.
 *
 * MariaDB prints a database name with its underscores escaped
 * (`fiatgodx\_toolkit`.*) and always in backticks, so stripping both once here
 * is what lets a plain comparison against the configured name match at all.
 * A PROXY grant matches the pattern as well, but names the target `''@''`, which
 * no database or table lookup will ever ask about; it is kept in the map and can
 * never affect a privilege decision. A line that is not a GRANT at all is skipped.
 *
 * @param array<int,string> $lines
 * @return array<string,array<int,string>> target => privileges
 */
function grant_objects(array $lines): array
{
    $objects = array();
    foreach ($lines as $line) {
        if (preg_match('/^GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+/i', (string) $line, $match) !== 1) {
            continue;
        }

        $privileges = array_map('trim', explode(',', $match[1]));
        $target = str_replace(array('\\', '`'), '', trim($match[2]));
        $objects[$target] = array_values(array_unique(
            array_merge(isset($objects[$target]) ? $objects[$target] : array(), $privileges)
        ));
    }

    return $objects;
}


/**
 * Which privileges the account holds on one table, from the GRANT targets it has.
 *
 * Pure, so it can be tested without a server: the caller passes the map that
 * SHOW GRANTS produced, already normalised to `*.*`, `db.*` and `db.table` keys.
 *
 * A grant on `db.*` or `*.*` covers the table, which is why all three scopes are
 * read rather than only the exact one: an account given ALL on the database has
 * ALTER on the table without any table-level line existing. "ALL PRIVILEGES" as
 * a word covers every privilege asked for, including ones MySQL adds later.
 *
 * @param array<string,array<int,string>> $objects
 * @param array<int,string> $wanted
 * @return array<string,bool> lowercased privilege => held, plus 'all'
 */
function privileges_on(
    array $objects,
    string $database,
    string $table,
    array $wanted = array('ALTER', 'DROP', 'CREATE', 'INSERT', 'SELECT', 'UPDATE')
): array {
    $scopes = array('*.*', $database . '.*', $database . '.' . $table);
    $held = array();
    foreach ($scopes as $scope) {
        if (!isset($objects[$scope]) || !is_array($objects[$scope])) {
            continue;
        }
        foreach ($objects[$scope] as $privilege) {
            $held[strtoupper(trim((string) $privilege))] = true;
        }
    }

    $all = isset($held['ALL PRIVILEGES']) || isset($held['ALL']);
    $answer = array('all' => $all);
    foreach ($wanted as $privilege) {
        $answer[strtolower($privilege)] = $all || isset($held[strtoupper($privilege)]);
    }

    return $answer;
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
 * The privileges this user actually holds, from SHOW GRANTS.
 *
 * A grant is the only evidence of what the binding may do; anything else is an
 * inference from having connected. MariaDB prints a database name with its
 * underscores escaped (`fiatgodx\_lovewebconnection`), so the targets are
 * normalised exactly as `db_sync.php?action=report` does it -- without that, every
 * line parses and then fails to match the database it names.
 *
 * The account's own password hash is stripped before anything is returned:
 * SHOW GRANTS does print it ("IDENTIFIED BY PASSWORD '*<hash>'"), so a grant line
 * is not safe to publish as-is, whatever it looks like.
 *
 * @return array<int,string> normalised GRANT statements, capped at 20
 */
function db_grants(string $label): array
{
    $link = db_named($label);
    if ($link === null) {
        return array();
    }

    try {
        $result = mysqli_query($link, 'SHOW GRANTS');
        if ($result === false) {
            return array();
        }
    } catch (Throwable $exc) {
        // Reachable but not able to say why: an empty list is the honest answer.
        return array();
    }

    $grants = array();
    while ($row = mysqli_fetch_row($result)) {
        $line = trim((string) $row[0]);
        if ($line === '') {
            continue;
        }
        $line = str_replace(array(chr(92), '`'), '', $line);
        // MariaDB prints the account's own password hash inside SHOW GRANTS
        // ("... IDENTIFIED BY PASSWORD '*<hash>'"). Removed here, once, before the
        // line can reach the JSON, the page, or anything that caches them.
        $grants[] = (string) preg_replace(
            '/\s+IDENTIFIED\s+(?:BY\s+PASSWORD|BY|WITH|VIA)\b.*$/i',
            '',
            $line
        );
        if (count($grants) >= 20) {
            break;
        }
    }
    mysqli_free_result($result);

    return $grants;
}

/**
 * Domain -> database, from config.local.php `domains`.
 *
 * cPanel has no "attach a database to a domain" switch: a database is bound to a
 * site by whatever config the code in that document root reads. This is that
 * config, stated once and checkable, instead of the same three credentials pasted
 * into four applications.
 *
 * A domain mapped to a label nobody configured is reported with `database` null
 * rather than dropped: a typo that silently does nothing is worse than one that
 * says it did nothing.
 *
 * @return array<string,array<string,mixed>>
 */
function domain_map(): array
{
    $config = db_config();
    if ($config === null || empty($config['domains']) || !is_array($config['domains'])) {
        return array();
    }

    $labels = db_labels();
    $overview = db_overview();
    $map = array();

    foreach ($config['domains'] as $domain => $label) {
        $domain = strtolower(trim((string) $domain));
        $label = (string) $label;
        if ($domain === '') {
            continue;
        }
        $entry = isset($overview[$label]) ? $overview[$label] : null;
        $map[$domain] = array(
            'label' => $label,
            'database' => isset($labels[$label]) ? $labels[$label] : null,
            'on' => $entry === null ? 0 : (int) $entry['on'],
            'tables' => $entry === null ? 0 : (int) $entry['table_count'],
            'bound' => $entry !== null,
        );
    }

    return $map;
}

/**
 * The label that every domain not named explicitly falls back to, or null.
 *
 * This account has 2,046 domains. Binding them one line at a time would be
 * 2,046 entries that all say the same thing and a config nobody will maintain;
 * a default rule binds the whole set, including domains added tomorrow, and
 * explicit entries above it still win.
 */
function db_default_label(): ?string
{
    $config = db_config();
    if ($config === null || empty($config['default'])) {
        return null;
    }

    $label = (string) $config['default'];
    $labels = db_labels();

    return isset($labels[$label]) ? $label : null;
}

/**
 * One aggregate over every domain binding, so "is everything connected?" is a
 * single answer instead of a table somebody has to read.
 *
 * `first` and `last` are the first and last integers of the connection vector
 * (one bit per domain, in config order); `vector` is that whole vector as a
 * string. Together they let a caller check both ends and the whole set at once
 * without agreeing on a different convention: all_on is 1 only when every domain
 * in the vector is 1, so a gap anywhere -- including the middle -- fails it.
 *
 * @return array<string,mixed>
 */
function domain_synergy(): array
{
    $bits = array();
    $explicit = 0;
    foreach (domain_map() as $binding) {
        $bits[] = (int) $binding['on'];
        $explicit++;
    }

    // The default rule covers every domain not named explicitly, so it is one
    // more bit: drop it and "4 of 4" would be true while 2,042 domains were
    // unattached.
    $default = db_default_label();
    if ($default !== null) {
        $labels = db_labels();
        $overview = db_overview();
        $entry = isset($overview[$default]) ? $overview[$default] : null;
        $bits[] = $entry === null ? 0 : (int) $entry['on'];
    }

    $count = count($bits);
    $on = 0;
    foreach ($bits as $bit) {
        $on += $bit === 1 ? 1 : 0;
    }
    $allOn = ($count > 0 && $on === $count) ? 1 : 0;

    return array(
        'domains' => $count,
        'connected' => $on,
        'first' => $count > 0 ? $bits[0] : 0,
        'last' => $count > 0 ? $bits[$count - 1] : 0,
        'vector' => implode('', $bits),
        'explicit' => $explicit,
        'default' => $default,
        'all_on' => $allOn,
        'on' => $allOn,
    );
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
 * The table that holds a text file from the document root, byte for byte.
 *
 * cPanel gives this account no shell, so the only PHP that can reach the
 * database is the PHP served here; cjd_store.php inserts into this table and
 * this function makes sure the table exists first.
 *
 * LONGTEXT rather than TEXT: a 64 KiB TEXT column is a 65,535-byte ceiling and
 * the file being stored is 65,154 bytes -- close enough that the next edit would
 * hit it, and a silent truncation is the one failure this table must not have.
 */
function ensure_cjd_table(mysqli $link): void
{
    mysqli_query(
        $link,
        'CREATE TABLE IF NOT EXISTS cjd_source ('
        . ' id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,'
        . ' filename VARCHAR(64) NOT NULL,'
        . ' bytes INT UNSIGNED NOT NULL,'
        . ' sha256 CHAR(64) NOT NULL,'
        . ' content LONGTEXT NOT NULL,'
        . ' stored_at DATETIME NOT NULL,'
        . ' UNIQUE KEY filename_idx (filename)'
        . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
}

/**
 * The table that holds every row downloaded from every database, merged.
 *
 * Keyed on the row's identity rather than on a surrogate id: the same row found
 * in two databases has to merge to one entry, and an auto-increment id would
 * give you two. `seen_in` keeps how many places the row turned up, so "merged"
 * does not quietly claim the row only ever existed once.
 *
 * LONGTEXT, for the same reason as cjd_source: one of these rows carries a whole
 * stored file, and a TEXT column stops at 65,535 bytes.
 *
 * `run_token` records which upload wrote the row last. Rows are never deleted,
 * so a row removed from its source stays here for good; the token is what lets a
 * sync prove "everything I just sent is stored" without also claiming the table
 * holds nothing else, which it never will again.
 */
function ensure_merged_table(mysqli $link): void
{
    mysqli_query(
        $link,
        'CREATE TABLE IF NOT EXISTS merged_rows ('
        . ' id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,'
        . ' key_sha256 CHAR(64) NOT NULL,'
        . ' source_table VARCHAR(64) NOT NULL,'
        . ' source_db VARCHAR(64) NOT NULL,'
        . ' seen_in INT UNSIGNED NOT NULL DEFAULT 1,'
        . ' row_bytes INT UNSIGNED NOT NULL,'
        . ' row_json LONGTEXT NOT NULL,'
        . ' first_seen DATETIME NOT NULL,'
        . ' last_seen DATETIME NOT NULL,'
        . ' run_token VARCHAR(64) NULL,'
        . ' UNIQUE KEY key_idx (key_sha256),'
        . ' KEY source_idx (source_db),'
        . ' KEY run_idx (run_token)'
        . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );

    // A table created before run_token existed keeps the old shape: CREATE TABLE
    // IF NOT EXISTS has nothing to do on a table that is already there. Ask the
    // catalogue and add the column once. Failing here is fatal on purpose -- a
    // merge that cannot mark its own rows cannot be verified afterwards.
    $present = mysqli_query($link, "SHOW COLUMNS FROM merged_rows LIKE 'run_token'");
    if ($present === false) {
        throw new RuntimeException('merged_rows run_token cannot be inspected');
    }
    $has_run = mysqli_num_rows($present) > 0;
    mysqli_free_result($present);

    if (!$has_run && mysqli_query(
        $link,
        'ALTER TABLE merged_rows ADD COLUMN run_token VARCHAR(64) NULL, ADD KEY run_idx (run_token)'
    ) === false) {
        throw new RuntimeException('merged_rows run_token cannot be added');
    }
}

/**
 * What the database holds for the stored text file, without the text itself.
 *
 * Read-only and credential-free, so preview.php can publish it: the sha256 is
 * what turns "I uploaded it" into a checkable claim, and it costs one row read
 * instead of dragging 65 KB of body through every preview request.
 *
 * @return array<string,mixed>
 */
function cjd_status(): array
{
    $status = array(
        'stored' => false,
        'on' => 0,
        'file' => null,
        'bytes' => 0,
        'sha256' => null,
        'stored_at' => null,
    );

    $link = db();
    if ($link === null) {
        return $status;
    }

    try {
        $result = mysqli_query(
            $link,
            'SELECT filename, bytes, sha256, stored_at FROM cjd_source ORDER BY id DESC LIMIT 1'
        );
        if ($result !== false) {
            $row = mysqli_fetch_assoc($result);
            if (is_array($row)) {
                $status = array(
                    'stored' => true,
                    'on' => 1,
                    'file' => (string) $row['filename'],
                    'bytes' => (int) $row['bytes'],
                    'sha256' => (string) $row['sha256'],
                    'stored_at' => (string) $row['stored_at'],
                );
            }
            mysqli_free_result($result);
        }
    } catch (Throwable $exc) {
        // No table yet is not an error: the page still renders, stored stays 00.
    }

    return $status;
}

/**
 * What the merged table holds, without a single row of content.
 *
 * `digest` is the SHA-256 of every row key in sorted order. That is the point of
 * it: a caller can prove the table holds exactly the set it just uploaded
 * without dragging 65 KB rows back over the network to diff them, and the digest
 * it computes from its own keys either matches or the merge did not land. A row
 * count alone cannot tell "the same three rows" from "three other rows".
 *
 * @return array<string,mixed>
 */
function merged_status(?string $run = null): array
{
    $status = array(
        'table' => 'merged_rows',
        'on' => 0,
        'run_token' => $run,
        'rows' => 0,
        'bytes' => 0,
        'digest' => null,
        'last_seen' => null,
        'sources' => array(),
        'totals' => array('rows' => 0, 'bytes' => 0),
    );

    $link = db();
    if ($link === null) {
        return $status;
    }

    try {
        ensure_merged_table($link);

        // One optional filter, so the same function answers two different
        // questions: "what did that upload store" when a run is named, and "what
        // does the table hold in total" when it is not. Escaped rather than
        // bound: the value is a hex token and a single condition does not need a
        // prepared statement.
        $filter = '';
        if ($run !== null) {
            $filter = " WHERE run_token = '" . mysqli_real_escape_string($link, $run) . "'";
        }

        $result = mysqli_query(
            $link,
            'SELECT COUNT(*) AS rows_total, COALESCE(SUM(row_bytes), 0) AS bytes_total,'
            . ' MAX(last_seen) AS last_seen FROM merged_rows' . $filter
        );
        if ($result !== false) {
            $row = mysqli_fetch_assoc($result);
            if (is_array($row)) {
                $status['rows'] = (int) $row['rows_total'];
                $status['bytes'] = (int) $row['bytes_total'];
                $status['last_seen'] = $row['last_seen'] === null ? null : (string) $row['last_seen'];
            }
            mysqli_free_result($result);
        }

        // Keys only: 64 bytes per row instead of a whole row, and the digest
        // proves which rows, not merely how many.
        $context = hash_init('sha256');
        $result = mysqli_query($link, 'SELECT key_sha256 FROM merged_rows' . $filter . ' ORDER BY key_sha256');
        if ($result !== false) {
            while ($row = mysqli_fetch_row($result)) {
                hash_update($context, (string) $row[0]);
            }
            mysqli_free_result($result);
        }
        $status['digest'] = hash_final($context);
        $status['on'] = 1;

        $result = mysqli_query(
            $link,
            'SELECT source_db, source_table, COUNT(*) AS rows_total FROM merged_rows'
            . $filter
            . ' GROUP BY source_db, source_table ORDER BY source_db, source_table'
        );
        if ($result !== false) {
            while ($row = mysqli_fetch_assoc($result)) {
                $status['sources'][] = array(
                    'database' => (string) $row['source_db'],
                    'table' => (string) $row['source_table'],
                    'rows' => (int) $row['rows_total'],
                );
            }
            mysqli_free_result($result);
        }

        // The total is always unfiltered, even when the figures above are one
        // run's: the caller needs both to tell "stored exactly what I sent" from
        // "the table still holds what earlier runs put there".
        $result = mysqli_query(
            $link,
            'SELECT COUNT(*) AS rows_total, COALESCE(SUM(row_bytes), 0) AS bytes_total FROM merged_rows'
        );
        if ($result !== false) {
            $row = mysqli_fetch_assoc($result);
            if (is_array($row)) {
                $status['totals'] = array(
                    'rows' => (int) $row['rows_total'],
                    'bytes' => (int) $row['bytes_total'],
                );
            }
            mysqli_free_result($result);
        }
    } catch (Throwable $exc) {
        // No table yet is not an error: the first merge creates it.
        $status['on'] = 0;
    }

    return $status;
}

/**
 * The table that holds the chain each label is connected to.
 *
 * One row per label, so the interface asks the database "what is
 * lovewebconnection connected to" and gets back a stored answer. Two bits are
 * kept separately and deliberately: `enabled` is whether the connection is
 * configured and `on` is whether it is live. Collapsing them into one column
 * makes "switched off on purpose" indistinguishable from "never set up".
 *
 * The row lives in the primary database because that is the only database this
 * user may write to -- `lovewebconnection` is SELECT-only, which
 * `db_sync.php?action=report` proves from SHOW GRANTS. `label` is what links the
 * two, and the interface reports `storage` so the difference is never guessed at.
 */
function ensure_chain_table(mysqli $link): void
{
    mysqli_query(
        $link,
        'CREATE TABLE IF NOT EXISTS chain_connections ('
        . ' id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,'
        . ' label VARCHAR(64) NOT NULL,'
        . ' network VARCHAR(32) NOT NULL,'
        . ' chain_id BIGINT UNSIGNED NOT NULL,'
        . ' rpc_url VARCHAR(255) NOT NULL,'
        . ' currency VARCHAR(16) NOT NULL,'
        . ' explorer VARCHAR(255) NOT NULL,'
        . ' gateway VARCHAR(64) NOT NULL,'
        . ' enabled TINYINT(1) NOT NULL DEFAULT 1,'
        // `on` is a reserved word in MariaDB: unquoted, the whole CREATE fails and
        // every read silently falls back to defaults, so it is quoted everywhere.
        . ' `on` TINYINT(1) NOT NULL DEFAULT 1,'
        . ' updated_at DATETIME NOT NULL,'
        . ' UNIQUE KEY label_idx (label)'
        . ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
}

/** The stored connection for the interface's label, or null when there is none.
 *
 * @return array<string,mixed>|null
 */
function chain_row(mysqli $link): ?array
{
    $label = CHAIN_LABEL;
    $statement = mysqli_prepare(
        $link,
        'SELECT label, network, chain_id, rpc_url, currency, explorer, gateway, enabled, `on`, updated_at'
        . ' FROM chain_connections WHERE label = ? LIMIT 1'
    );
    mysqli_stmt_bind_param($statement, 's', $label);
    mysqli_stmt_execute($statement);
    $result = mysqli_stmt_get_result($statement);
    $row = mysqli_fetch_assoc($result);
    mysqli_stmt_close($statement);

    return is_array($row) ? $row : null;
}

/**
 * Write the connection row, switched on.
 *
 * `on = 1` here is the upgrade: a fresh install and a re-run both land on "live",
 * and an existing row is pulled to `enabled = 1, on = 1` rather than left as it
 * was. Switching the interface off afterwards is an UPDATE, not a missing feature.
 */
function chain_seed(mysqli $link): void
{
    $label = CHAIN_LABEL;
    $network = CHAIN_NETWORK;
    $chainId = CHAIN_ID;
    $rpc = CHAIN_RPC;
    $currency = CHAIN_CURRENCY;
    $explorer = CHAIN_EXPLORER;
    $gateway = CHAIN_GATEWAY;

    $statement = mysqli_prepare(
        $link,
        'INSERT INTO chain_connections'
        . ' (label, network, chain_id, rpc_url, currency, explorer, gateway, enabled, `on`, updated_at)'
        . ' VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, UTC_TIMESTAMP())'
        . ' ON DUPLICATE KEY UPDATE enabled = 1, `on` = 1, updated_at = UTC_TIMESTAMP()'
    );
    mysqli_stmt_bind_param($statement, 'ssissss', $label, $network, $chainId, $rpc, $currency, $explorer, $gateway);
    mysqli_stmt_execute($statement);
    mysqli_stmt_close($statement);
}

/**
 * The `web9999` host group: every bound host whose first label is w*9999.
 *
 * `web9999` is not itself a bound domain -- the account holds `ww9999.metamask.
 * x12.mx` and 27 more like it -- so the group is matched on the shape rather than
 * on one literal name that would match nothing. `all_on` follows the same rule as
 * `domain_synergy`: 1 only when every member is 1, so a gap inside the group is
 * not hidden by its ends.
 *
 * @return array<string,mixed>
 */
function chain_gateway(): array
{
    $gateway = array(
        'name' => CHAIN_GATEWAY,
        'pattern' => 'w*9999.',
        'matches' => 0,
        'on' => 0,
        'all_on' => 0,
        'first' => null,
        'last' => null,
        'domains' => array(),
    );

    foreach (domain_map() as $domain => $binding) {
        if (preg_match('/^(web|w+)9999\./i', (string) $domain) !== 1) {
            continue;
        }
        $gateway['domains'][] = array(
            'domain' => (string) $domain,
            'on' => (int) $binding['on'],
            'database' => $binding['database'],
        );
        $gateway['on'] += (int) $binding['on'];
    }

    $count = count($gateway['domains']);
    $gateway['matches'] = $count;
    if ($count > 0) {
        $gateway['first'] = $gateway['domains'][0]['domain'];
        $gateway['last'] = $gateway['domains'][$count - 1]['domain'];
    }
    $gateway['all_on'] = ($count > 0 && $gateway['on'] === $count) ? 1 : 0;

    return $gateway;
}

/**
 * The blockchain interface's state, without touching the network.
 *
 * Kept separate from the RPC reads on purpose: `preview.php` publishes this on
 * every request, and a preview that took 20 seconds because a public RPC was slow
 * would turn a status page into an outage.
 *
 * @return array<string,mixed>
 */
function chain_status(): array
{
    $status = array(
        'interface' => 'blockchain',
        'version' => 1,
        'label' => CHAIN_LABEL,
        'network' => CHAIN_NETWORK,
        'chain_id' => CHAIN_ID,
        'rpc' => CHAIN_RPC,
        'currency' => CHAIN_CURRENCY,
        'explorer' => CHAIN_EXPLORER,
        'enabled' => 0,
        // Binary, like every other binding on this account: 01 = live.
        'on' => 0,
        'source' => 'defaults',
        'updated_at' => null,
        'storage' => array('table' => 'chain_connections', 'database' => null),
        'bound' => array(
            'label' => CHAIN_LABEL,
            'on' => 0,
            'connected' => false,
            'tables' => 0,
            'table_list' => array(),
            'detail' => 'not reachable',
            'grants' => array(),
        ),
        'gateway' => chain_gateway(),
        'domains' => array(),
    );

    $overview = db_overview();
    if (isset($overview[CHAIN_LABEL])) {
        // The count alone could not answer the question the page was asked: a
        // binding that connected and can see nothing reads the same as one with
        // four tables. Carry the tables themselves and the grant lines through, so
        // "connected" is accompanied by what it can actually read and do.
        $status['bound'] = array(
            'label' => CHAIN_LABEL,
            'on' => (int) $overview[CHAIN_LABEL]['on'],
            'connected' => (bool) $overview[CHAIN_LABEL]['connected'],
            'tables' => (int) $overview[CHAIN_LABEL]['table_count'],
            'table_list' => $overview[CHAIN_LABEL]['tables'],
            'detail' => (string) $overview[CHAIN_LABEL]['detail'],
            'grants' => db_grants(CHAIN_LABEL),
        );
    }

    $synergy = domain_synergy();
    $status['domains'] = array(
        'total' => (int) $synergy['domains'],
        'on' => (int) $synergy['connected'],
        'all_on' => (int) $synergy['all_on'],
    );

    // The .network hosts as one group of their own. "Every domain is on" and "every
    // .network host is on" are different claims: the first can be true while the
    // second is not, and a group that reports its own count is the only way to see
    // which of the two you actually have.
    //
    // Keyed `network_hosts`, not `network`: `network` is already this chain's name
    // ("polygon"), and reusing the key left the later row read overwriting the
    // group with a string while chain.php went on treating it as an array.
    $networkTotal = 0;
    $networkOn = 0;
    foreach (domain_map() as $domain => $binding) {
        if (substr(strtolower((string) $domain), -8) !== '.network') {
            continue;
        }
        $networkTotal++;
        $networkOn += (int) $binding['on'];
    }
    $status['network_hosts'] = array(
        'suffix' => '.network',
        'total' => $networkTotal,
        'on' => $networkOn,
        'all_on' => ($networkTotal > 0 && $networkOn === $networkTotal) ? 1 : 0,
    );

    $link = db();
    if ($link === null) {
        return $status;
    }

    try {
        ensure_chain_table($link);
        $labels = db_labels();
        $status['storage']['database'] = isset($labels[DB_PRIMARY]) ? $labels[DB_PRIMARY] : null;

        $row = chain_row($link);
        if ($row === null) {
            // Seed once, then read it back: the reply should describe the row that
            // is stored, not the one this file hoped to store.
            chain_seed($link);
            $row = chain_row($link);
        }
        if ($row === null) {
            return $status;
        }

        $status['network'] = (string) $row['network'];
        $status['chain_id'] = (int) $row['chain_id'];
        $status['rpc'] = (string) $row['rpc_url'];
        $status['currency'] = (string) $row['currency'];
        $status['explorer'] = (string) $row['explorer'];
        $status['enabled'] = (int) $row['enabled'];
        $status['on'] = ((int) $row['enabled'] === 1 && (int) $row['on'] === 1) ? 1 : 0;
        unset($status['storage']['error']);
        $status['updated_at'] = (string) $row['updated_at'];
        $status['source'] = 'database';
    } catch (Throwable $exc) {
        // A database problem must not take the page down -- but it must not pass
        // for success either. The defaults stand, `source` keeps saying they are
        // defaults, and storage says why. A silently swallowed error here once
        // left the interface reporting "off" while its table had been rejected
        // for using the reserved word `on` as a column name.
        $status['storage']['error'] = 'chain table not ready';
    }

    return $status;
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
