<?php
declare(strict_types=1);

/**
 * Broadcast already-signed transactions over JSON-RPC. PHP port of broadcast.py.
 *
 * Same safety property as the Python module: this file has no key and cannot
 * sign. It only ever resends the exact bytes it was given, so a retry cannot
 * produce a second payment -- one signature carries one nonce.
 *
 * Requires: PHP 7.4+ with ext-curl. No composer dependencies, because a shared
 * host is exactly where you cannot install them.
 *
 *   $rpc = new JsonRpc('https://polygon-amoy-bor-rpc.publicnode.com');
 *   $caster = new Broadcaster($rpc, new Throttle(0.5), new Journal('/tmp/b.jsonl'));
 *   $out = $caster->broadcast($rawHex, $txHash);
 *   if (!$out->ok()) { echo $out->error; }
 *
 * NOTE ON HASHES: core PHP has no keccak-256, so a transaction hash cannot be
 * computed here. Pass the hash in (Python's Web3.to_hex(signed.hash) is the
 * source of truth). If it is omitted the journal records a sha256 fingerprint
 * instead, clearly marked, so the file never pretends to hold a real tx hash.
 */

final class RpcError extends RuntimeException
{
}

final class JsonRpc
{
    /** @var string */
    private $url;
    /** @var int */
    private $timeout;

    public function __construct(string $url, int $timeout = 30)
    {
        $this->url = $url;
        $this->timeout = $timeout;
    }

    /**
     * One JSON-RPC call. Throws RpcError on transport, HTTP or JSON-RPC error.
     *
     * @param array<int,mixed> $params
     * @return mixed
     */
    public function call(string $method, array $params = array())
    {
        $payload = json_encode(array(
            'jsonrpc' => '2.0',
            'id' => 1,
            'method' => $method,
            'params' => $params,
        ));
        if ($payload === false) {
            throw new RpcError('could not encode the request');
        }

        $ch = curl_init($this->url);
        curl_setopt_array($ch, array(
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $payload,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_TIMEOUT => $this->timeout,
            CURLOPT_HTTPHEADER => array('Content-Type: application/json'),
            CURLOPT_USERAGENT => 'loveweb-wallet-toolkit-php/1.0',
        ));
        $body = curl_exec($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $transport = curl_error($ch);
        curl_close($ch);

        if ($body === false) {
            throw new RpcError('transport: ' . $transport);
        }
        // 403 is transient here on purpose: a public Polygon endpoint was seen
        // answering 403 when it was rate limiting, not when the request was bad.
        if ($status === 403 || $status === 429 || $status >= 500) {
            throw new RpcError('HTTP ' . $status);
        }

        $decoded = json_decode((string) $body, true);
        if (!is_array($decoded)) {
            throw new RpcError('HTTP ' . $status . ' with a non-JSON body');
        }
        if (isset($decoded['error'])) {
            $message = 'unknown JSON-RPC error';
            if (isset($decoded['error']['message'])) {
                $message = (string) $decoded['error']['message'];
            }
            throw new RpcError($message);
        }

        return array_key_exists('result', $decoded) ? $decoded['result'] : null;
    }
}

/** Outcome of one signed transaction. */
final class Outcome
{
    /** @var string */
    public $txHash;
    /** @var string sent | already_known | failed | skipped | duplicate */
    public $status;
    /** @var int */
    public $attempts;
    /** @var string */
    public $error;

    public function __construct(string $txHash, string $status, int $attempts = 0, string $error = '')
    {
        $this->txHash = $txHash;
        $this->status = $status;
        $this->attempts = $attempts;
        $this->error = $error;
    }

    /** True when the network holds the transaction, however it got there. */
    public function ok(): bool
    {
        return $this->status === 'sent' || $this->status === 'already_known';
    }
}

/** Spaces sends out, and widens the gap when the endpoint pushes back. */
final class Throttle
{
    /** @var float */
    public $minInterval;
    /** @var float */
    public $maxInterval;
    /** @var float */
    public $factor;
    /** @var float */
    public $interval;
    /** @var float|null */
    private $last = null;

    public function __construct(float $minInterval = 0.25, float $maxInterval = 8.0, float $factor = 2.0)
    {
        $this->minInterval = $minInterval;
        $this->maxInterval = $maxInterval;
        $this->factor = $factor;
        $this->interval = $minInterval;
    }

    public function wait(): float
    {
        if ($this->last !== null) {
            $delay = $this->last + $this->interval - microtime(true);
            if ($delay > 0) {
                usleep((int) round($delay * 1000000));
            }
        }
        $this->last = microtime(true);
        return $this->interval;
    }

    public function pushBack(): float
    {
        $this->interval = min($this->maxInterval, $this->interval * $this->factor);
        return $this->interval;
    }

    public function relax(): float
    {
        $this->interval = max($this->minInterval, $this->interval / $this->factor);
        return $this->interval;
    }
}

/** Append-only JSONL journal, written and flushed BEFORE each send. */
final class Journal
{
    /** @var string */
    private $path;

    public function __construct(string $path)
    {
        $this->path = $path;
    }

    /** @param array<string,mixed> $extra */
    public function record(string $txHash, string $status, int $attempts = 0, string $error = '', array $extra = array()): void
    {
        $row = array_merge(array(
            'tx_hash' => $txHash,
            'status' => $status,
            'attempts' => $attempts,
            'error' => $error,
            'at' => gmdate('c'),
        ), $extra);

        $line = json_encode($row);
        if ($line === false) {
            return;
        }
        $handle = @fopen($this->path, 'a');
        if ($handle === false) {
            return; // a journal that cannot be written must not stop a send
        }
        if (flock($handle, LOCK_EX)) {
            fwrite($handle, $line . "\n");
            fflush($handle);
            flock($handle, LOCK_UN);
        }
        fclose($handle);
    }

    /** @return array<string,array<string,mixed>> newest record per hash */
    public function load(): array
    {
        $out = array();
        if (!is_file($this->path)) {
            return $out;
        }
        $lines = file($this->path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if ($lines === false) {
            return $out;
        }
        foreach ($lines as $line) {
            $row = json_decode($line, true);
            if (is_array($row) && isset($row['tx_hash'])) {
                $out[(string) $row['tx_hash']] = $row;
            }
        }
        return $out;
    }
}

/** Sends signed transactions, retrying only what is worth retrying. */
final class Broadcaster
{
    /** @var string[] */
    private static $alreadyKnown = array(
        'already known',
        'known transaction',
        'already imported',
        'already exists',
        'already in pool',
        'nonce too low',
    );

    /** @var string[] */
    private static $transient = array(
        '429', 'too many requests', 'rate limit', 'ratelimit', '403', 'forbidden',
        '502', '503', '504', 'server error', 'bad gateway', 'gateway timeout',
        'service unavailable', 'temporarily', 'timed out', 'timeout',
        'connection', 'reset by peer', 'broken pipe',
    );

    /** @var JsonRpc */
    private $rpc;
    /** @var Throttle|null */
    private $throttle;
    /** @var Journal|null */
    private $journal;
    /** @var int */
    private $attempts;
    /** @var float */
    private $baseDelay;

    public function __construct(JsonRpc $rpc, ?Throttle $throttle = null, ?Journal $journal = null, int $attempts = 4, float $baseDelay = 1.0)
    {
        if ($attempts < 1) {
            throw new InvalidArgumentException('attempts must be at least 1');
        }
        $this->rpc = $rpc;
        $this->throttle = $throttle;
        $this->journal = $journal;
        $this->attempts = $attempts;
        $this->baseDelay = $baseDelay;
    }

    /** 'already_known', 'transient' or 'fatal' for one send failure. */
    public static function classify(string $message): string
    {
        $text = strtolower($message);
        foreach (self::$alreadyKnown as $marker) {
            if (strpos($text, $marker) !== false) {
                return 'already_known';
            }
        }
        foreach (self::$transient as $marker) {
            if (strpos($text, $marker) !== false) {
                return 'transient';
            }
        }
        return 'fatal';
    }

    /**
     * Send one signed transaction. Never throws on a send failure: the caller
     * gets an Outcome, because "the endpoint rate limited us" must not look
     * like "nothing was sent".
     */
    public function broadcast(string $rawHex, ?string $txHash = null): Outcome
    {
        $raw = strpos($rawHex, '0x') === 0 ? substr($rawHex, 2) : $rawHex;
        if ($raw === '' || !ctype_xdigit($raw)) {
            return new Outcome($txHash === null ? '' : $txHash, 'failed', 0, 'not hex: refusing to send');
        }
        if ($txHash === null) {
            // No keccak in core PHP: label the stand-in so nobody mistakes it.
            $txHash = 'sha256:' . hash('sha256', $raw);
        }

        if ($this->journal !== null) {
            $this->journal->record($txHash, 'sending');
        }
        if ($this->throttle !== null) {
            $this->throttle->wait();
        }

        $lastError = '';
        for ($attempt = 1; $attempt <= $this->attempts; $attempt++) {
            try {
                $this->rpc->call('eth_sendRawTransaction', array('0x' . $raw));
            } catch (RpcError $exc) {
                $message = $exc->getMessage();
                $lastError = substr($message, 0, 300);
                $kind = self::classify($message);

                if ($kind === 'already_known') {
                    if ($this->journal !== null) {
                        $this->journal->record($txHash, 'already_known', $attempt, $lastError);
                    }
                    return new Outcome($txHash, 'already_known', $attempt, $lastError);
                }
                if ($kind === 'fatal') {
                    if ($this->journal !== null) {
                        $this->journal->record($txHash, 'failed', $attempt, $lastError);
                    }
                    return new Outcome($txHash, 'failed', $attempt, $lastError);
                }
                if ($this->throttle !== null) {
                    $this->throttle->pushBack();
                }
                if ($attempt < $this->attempts) {
                    $delay = $this->baseDelay * pow(2, $attempt - 1);
                    usleep((int) round(min(30.0, $delay) * 1000000));
                    continue;
                }
            }

            if ($this->throttle !== null) {
                $this->throttle->relax();
            }
            if ($this->journal !== null) {
                $this->journal->record($txHash, 'sent', $attempt);
            }
            return new Outcome($txHash, 'sent', $attempt);
        }

        if ($this->journal !== null) {
            $this->journal->record($txHash, 'failed', $this->attempts, $lastError);
        }
        return new Outcome($txHash, 'failed', $this->attempts, $lastError);
    }

    /**
     * Send a batch in order, stopping at the first real failure.
     *
     * @param array<int,string> $rawHexes
     * @param array<int,string>|null $txHashes aligned with $rawHexes, optional
     * @return array<int,Outcome>
     */
    public function broadcastMany(array $rawHexes, ?array $txHashes = null): array
    {
        $outcomes = array();
        $seen = array();
        $stopped = null;
        $index = 0;

        foreach ($rawHexes as $rawHex) {
            $known = $txHashes !== null && isset($txHashes[$index]) ? (string) $txHashes[$index] : null;
            $hint = $known === null ? 'sha256:' . hash('sha256', ltrim($rawHex, '0x')) : $known;

            if ($stopped !== null) {
                $outcomes[] = new Outcome($hint, 'skipped', 0, 'not attempted: ' . $stopped);
                $index++;
                continue;
            }
            $key = strtolower(ltrim($rawHex, '0x'));
            if (isset($seen[$key])) {
                $outcomes[] = new Outcome($hint, 'duplicate', 0, 'identical signed transaction already in this batch');
                $index++;
                continue;
            }
            $seen[$key] = true;

            $outcome = $this->broadcast($rawHex, $known);
            $outcomes[] = $outcome;
            if ($outcome->status === 'failed') {
                $stopped = 'index ' . $index . ' failed: ' . substr($outcome->error, 0, 100);
            }
            $index++;
        }

        return $outcomes;
    }

    /** @param array<int,Outcome> $outcomes */
    public static function summarise(array $outcomes): string
    {
        $counts = array();
        foreach ($outcomes as $outcome) {
            $status = $outcome->status;
            $counts[$status] = isset($counts[$status]) ? $counts[$status] + 1 : 1;
        }
        ksort($counts);
        $parts = array();
        foreach ($counts as $status => $count) {
            $parts[] = $status . '=' . $count;
        }
        return implode('  ', $parts);
    }
}

/**
 * Exact decimal arithmetic on wei, kept as strings.
 *
 * hexdec() must NOT be used on a balance: PHP's int is 64-bit (max 9.22e18 wei,
 * about 9.2 POL), so anything larger silently becomes a float and loses its low
 * digits -- in a wallet, that is the digits you care about. Everything here is
 * string math, correct for the full 2^256 range.
 */
final class Wei
{
    /** Decimal string for a hex quantity. Never overflows, never rounds. */
    public static function fromHex(string $hex): string
    {
        $hex = strtolower(trim($hex));
        if (strpos($hex, '0x') === 0) {
            $hex = substr($hex, 2);
        }
        if ($hex === '' || !ctype_xdigit($hex)) {
            return '0';
        }
        $dec = '0';
        $length = strlen($hex);
        for ($i = 0; $i < $length; $i++) {
            $dec = self::mulSmall($dec, 16);
            $dec = self::add($dec, (string) hexdec($hex[$i]));
        }
        return $dec;
    }

    /** Sum of two decimal strings. */
    public static function add(string $a, string $b): string
    {
        $a = self::normalise($a);
        $b = self::normalise($b);
        $i = strlen($a) - 1;
        $j = strlen($b) - 1;
        $carry = 0;
        $out = '';
        while ($i >= 0 || $j >= 0 || $carry > 0) {
            $sum = ($i >= 0 ? (int) $a[$i] : 0) + ($j >= 0 ? (int) $b[$j] : 0) + $carry;
            $out .= (string) ($sum % 10);
            $carry = intdiv($sum, 10);
            $i--;
            $j--;
        }
        return $out === '' ? '0' : strrev($out);
    }

    /** Product of a decimal string and a small non-negative int. */
    public static function mulSmall(string $a, int $n): string
    {
        $a = self::normalise($a);
        if ($n <= 0 || $a === '0') {
            return '0';
        }
        $carry = 0;
        $out = '';
        for ($i = strlen($a) - 1; $i >= 0; $i--) {
            $product = (int) $a[$i] * $n + $carry;
            $out .= (string) ($product % 10);
            $carry = intdiv($product, 10);
        }
        while ($carry > 0) {
            $out .= (string) ($carry % 10);
            $carry = intdiv($carry, 10);
        }
        return strrev($out);
    }

    /** Decimal string scaled down by 10^$decimals, truncated to $places. */
    public static function scale(string $wei, int $decimals, int $places = 6): string
    {
        $wei = self::normalise($wei);
        $places = max(0, min($decimals, $places));
        $padded = str_pad($wei, $decimals + 1, '0', STR_PAD_LEFT);
        $cut = strlen($padded) - $decimals;
        $whole = ltrim(substr($padded, 0, $cut), '0');
        if ($whole === '') {
            $whole = '0';
        }
        if ($places === 0) {
            return $whole;
        }
        return $whole . '.' . substr($padded, $cut, $places);
    }

    /** Human amount like "0.001" into a wei decimal string. */
    public static function parseAmount(string $amount): string
    {
        $amount = trim($amount);
        if (!preg_match('/^[0-9]+(\.[0-9]+)?$/', $amount)) {
            return '0';
        }
        $parts = explode('.', $amount, 2);
        $whole = ltrim($parts[0], '0');
        $fraction = isset($parts[1]) ? substr(str_pad($parts[1], 18, '0'), 0, 18) : str_repeat('0', 18);
        $whole = $whole === '' ? '0' : $whole;
        $joined = $whole . $fraction;
        $trimmed = ltrim($joined, '0');
        return $trimmed === '' ? '0' : $trimmed;
    }

    /** Thousands separators for a plain integer string. */
    public static function thousands(string $value): string
    {
        $value = self::normalise($value);
        $out = '';
        $count = 0;
        for ($i = strlen($value) - 1; $i >= 0; $i--) {
            $out = $value[$i] . $out;
            $count++;
            if ($count % 3 === 0 && $i > 0) {
                $out = ',' . $out;
            }
        }
        return $out;
    }

    private static function normalise(string $value): string
    {
        $value = ltrim(trim($value), '0');
        return $value === '' ? '0' : $value;
    }
}
