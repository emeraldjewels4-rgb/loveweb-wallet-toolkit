<?php
declare(strict_types=1);

/**
 * The identity of one row: SHA-256 over the table name and its values.
 *
 * The table name is in the key so a row in `journal` is not the same row as an
 * identical one in `ledger`; the database name is deliberately NOT in it, so the
 * same row found in two databases merges to one row rather than two.
 *
 * Keys are sorted first: two databases return the same columns in whatever order
 * their schema declares, and unsorted, that difference alone would look like two
 * different rows.
 *
 * JSON_PRESERVE_ZERO_FRACTION is load-bearing, not decoration: without it
 * json_encode writes a float 1.0 as `1`, the same bytes an integer 1 produces, so
 * a REAL column and an INT column holding the same value would collide into one
 * row. With it they key as `1.0` and `1`, which is what Python's json module
 * writes too -- test_row_key_parity.py holds both sides to that.
 *
 * Why this is not inline in db_sync.php, where it is used: the route executes on
 * include, so anything that wants to check the key format against a second
 * implementation has to load a file that does nothing but define it. This is the
 * same reason grant_objects() lives in db.php. test_row_key_parity.py is the
 * caller that needed it, and it is also why the values here are typed the way
 * mysqli hands them over -- as strings -- while the parity test checks the
 * numbers and nulls the caller may still send.
 *
 * @param array<string,mixed> $values
 */
function row_key(string $table, array $values): string
{
    ksort($values);

    return hash(
        'sha256',
        $table . "\n" . json_encode($values, JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION)
    );
}
