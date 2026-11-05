<?php
declare(strict_types=1);

/**
 * Store a plain-text file that sits in this directory as one row in the primary
 * database, and say what the database actually holds afterwards.
 *
 *   curl -H 'X-Import-Key: <key>' https://.../toolkit/cjd_store.php?file=cjd.php
 *
 * Why a route and not a migration: there is no shell on this host, so the only
 * way to run PHP where the database lives is to request it. This route is a
 * write, so it is the one file here that must not be open to the world:
 *
 *   - it does nothing unless import.key exists next to it and the request
 *     presents that value (header preferred; ?key= accepted for a browser);
 *   - the name must match a plain-filename allowlist pattern and resolve inside
 *     this directory, so a query string cannot read /etc or a sibling app;
 *   - it only ever writes text into cjd_source. It cannot run, rename or delete
 *     anything, and REPLACE is keyed on filename, so re-running it is a no-op
 *     rather than a second copy.
 *
 * The reply reports the row read back from the database -- bytes, sha256 and
 * character count -- so "did it land?" is answered by the database rather than
 * by a 200 from PHP.
 */

require_once __DIR__ . '/db.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

/** The import key, if one was installed on the host. Never echoed. */
function cjd_key(): ?string
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

$key = cjd_key();
if ($key === null) {
    refuse(503, 'import disabled: no import.key on the host');
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

$link = db();
if ($link === null) {
    refuse(503, 'database not reachable');
}

$name = isset($_GET['file']) ? (string) $_GET['file'] : 'cjd.php';
if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}$/', $name) || strpos($name, '..') !== false) {
    refuse(400, 'not a plain file name in this directory');
}

$path = __DIR__ . '/' . $name;
if (!is_file($path) || !is_readable($path)) {
    refuse(404, 'no such file in the toolkit directory');
}

$body = (string) file_get_contents($path);
$bytes = strlen($body);
$sha = hash('sha256', $body);

if ($bytes === 0) {
    refuse(400, 'refusing to store an empty file');
}

try {
    ensure_cjd_table($link);
} catch (Throwable $exc) {
    refuse(503, 'could not prepare the cjd_source table');
}

try {
    $statement = mysqli_prepare(
        $link,
        'REPLACE INTO cjd_source (filename, bytes, sha256, content, stored_at) VALUES (?, ?, ?, ?, UTC_TIMESTAMP())'
    );
    mysqli_stmt_bind_param($statement, 'siss', $name, $bytes, $sha, $body);
    mysqli_stmt_execute($statement);
    mysqli_stmt_close($statement);
} catch (Throwable $exc) {
    // The message can name the table but never the credentials; still, keep the
    // public reply free of server detail and put the reason in the status field.
    refuse(500, 'the insert was refused by the database');
}

$stored = array();
try {
    $statement = mysqli_prepare(
        $link,
        'SELECT filename, bytes, sha256, CHAR_LENGTH(content) AS chars, stored_at'
        . ' FROM cjd_source WHERE filename = ? LIMIT 1'
    );
    mysqli_stmt_bind_param($statement, 's', $name);
    mysqli_stmt_execute($statement);
    $result = mysqli_stmt_get_result($statement);
    if ($result !== false) {
        $stored = mysqli_fetch_assoc($result) ?: array();
    }
    mysqli_stmt_close($statement);
} catch (Throwable $exc) {
    $stored = array();
}

$match = isset($stored['sha256']) && hash_equals((string) $stored['sha256'], $sha);

echo json_encode(array(
    'file' => $name,
    'bytes_sent' => $bytes,
    'sha256_sent' => $sha,
    'bytes_stored' => isset($stored['bytes']) ? (int) $stored['bytes'] : null,
    'sha256_stored' => isset($stored['sha256']) ? (string) $stored['sha256'] : null,
    'chars_stored' => isset($stored['chars']) ? (int) $stored['chars'] : null,
    'stored_at' => isset($stored['stored_at']) ? (string) $stored['stored_at'] : null,
    'match' => $match ? 1 : 0,
    'on' => $match ? 1 : 0,
), JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
