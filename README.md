# report-bank

`PythReportBank` stores the newest verified [Pyth Lazer](https://docs.pyth.network/price-feeds/pro)
price of every stream. A stream is one feed on one channel. Anyone can deliver a signed update, and
any number of contracts can read the stored prices.

```
latest[feed_id][channel] = Report(price, observation_us)
```

The price has 18 decimals (1e18 = $1). The observation time is Pyth's, in microseconds. The two
fields have a storage slot each: unpacked storage costs more gas but is simpler to read.

## Contract

| Call | What it does |
|---|---|
| `deliver_single_report(update)` | Takes a signed update of 1 to 8 feeds on one channel, with exactly `verification_fee()` as the value. Pyth's verifier checks the signature. Each feed is stored if it is newer than its stream. |
| `deliver_batch(updates)` | Takes up to 8 signed updates, for example on several channels or signed separately, with exactly `verification_fee()` per update as the value. It handles them in order, as that many single deliveries, and a malformed or unverified update reverts the whole batch. An empty batch does nothing. |
| `latest(feed_id, channel)` | The public mapping itself: `(price, observation_us)` of the stream's newest report, or `(0, 0)` before the first one. |
| `decode(update, feed_id, channel)` | Reads one feed from an update without verifying the signature or storing anything. It checks the same structure as delivery. This is for quotes. |
| `verification_fee()` | The verifier's fee, in wei. |
| `set_verifier(verifier)` | Replaces the verifier. Owner only. The stored prices are kept. |

Delivery rules:

- **Reverts:** a malformed update reverts as a whole. That covers bad magic, a feed count outside
  1 to 8, a wrong length (including bytes after the payload, which Pyth's verifier would ignore),
  a duplicate feed, properties other than price, exponent and feed update time in that order, a
  message time more than 6 s ahead of the block, and an observation time later than its message
  time. Every feed is checked, including those that will be skipped.
- **Skips:** a feed is skipped, not stored, when its observation is not newer than the stream's, or
  it has a price the bank does not store (a mantissa of 0 or less, or an exponent outside
  [-18, 0]). Its other feeds are still stored.
- **Channels:** channels never share storage. Reports on one channel cannot displace another
  channel's.
- **Freshness:** the bank sets no maximum age. An old observation is stored when it is newer than
  its stream's, so a late keeper transaction still lands. `latest()` returns the newest report
  however old it is, so each reader checks the age it accepts.
- **Fees:** the value must be exactly `verification_fee()` per update, or the call reverts. The bank
  cannot take a refund, so an overpayment would otherwise stay with Pyth's verifier.

This is twocrypto-ng's `contracts/main/PythReportBank.vy` without its moving averages. The rest of its interface is unchanged, including `latest` and `decode`, which
`YBOraclizedPolicy` calls.

## Feeds

| Feed | Id |
|---|---|
| BTC/USD | 1 |
| ETH/USD | 2 |

Both use channel 4 (`fixed_rate@1000ms`). Our API key is refused anything faster.

## Setup

```sh
uv sync
echo 'PYTH_API_KEY=...' > .env   # needed only for live fetches
```

## Tests

```sh
uv run pytest
```

- **`tests/test_unit.py`:** updates built in Python and signed with a test key. Covers delivery,
  the future-message check, streams, price scaling, malformed input, batches, decoding and governance.
- **`tests/test_integration.py`:** a real signed BTC/USD + ETH/USD update recorded from Pyth
  (`tests/fixtures/btc_eth_usd.json`), replayed with the block clock set to its message time. The
  stored prices must equal Pyth's own parse. Tampering, a foreign signer, an expired signer and an
  early delivery are refused; a delivery an hour late is still stored. Whenever `PYTH_API_KEY` is set, a live test also
  fetches one update of both feeds, then each feed signed separately, and delivers those two as a
  batch.

Both suites verify signatures with `tests/mocks/PythLazer.vy`. It is a Vyper port of `verifyUpdate`
from Pyth's [PythLazer.sol](https://github.com/pyth-network/pyth-crosschain/blob/main/lazer/contracts/evm/src/PythLazer.sol)
0.3.0, so it does real ECDSA recovery and keeps the fee check, envelope checks, signer expiry and
refund. The integration tests trust only Pyth's mainnet signer `0x26FB61A864c758AE9fBA027a96010480658385B9`.

These tests are not a check of the deployed verifier
(`0xACeA761c27A909d4D3895128EBe6370FDE2dF481` on Ethereum). That would need a chain fork, which
needs an RPC URL.

To record a fresh fixture:

```sh
uv run python pyth.py
```
