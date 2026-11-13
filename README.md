# report-bank

`PythReportBank` stores the newest verified [Pyth Lazer](https://docs.pyth.network/price-feeds/pro)
price of every stream. A stream is one feed on one channel. Anyone can deliver a signed update, and
any number of contracts can read the stored prices.

Deployed on Ethereum at
[`0x76216954BE01eECdC0D7b00cd8bb72C2e28F0da7`](https://etherscan.io/address/0x76216954BE01eECdC0D7b00cd8bb72C2e28F0da7#code)
(2026-11-13), owned by the YieldBasis DAO. That deployment predates the 60-second age limit below,
which takes effect with the next deployment.

```
latest[feed_id][channel] = Report(price, observation_us)
```

The price has 18 decimals (1e18 = $1). The observation time is Pyth's, in microseconds. The two
fields have a storage slot each: unpacked storage costs more gas but is simpler to read.

## Contract

| Call | What it does |
|---|---|
| `deliver_report(update)` | Takes a signed update of 1 to 8 feeds on one channel, with exactly `verification_fee()` as the value. Pyth's verifier checks the signature. Its message and every feed must be from 60 s before to 6 s after the block. Each feed is stored if it is newer than its stream. Updates on several channels take a call each. |
| `latest(feed_id, channel)` | The public mapping itself: `(price, observation_us)` of the stream's newest report, or `(0, 0)` before the first one. |
| `decode(update, feed_id, channel)` | Converts one feed of an update to `(price, observation_us)`, for quotes. **Not protected:** it checks the structure as delivery does but not the signature, so anyone can pass any price. Never use its result to authorize anything; `latest` holds verified prices. |
| `verification_fee()` | The verifier's fee, in wei. |
| `set_verifier(verifier)` | Replaces the verifier. Owner only. The stored prices are kept. |

Delivery rules:

- **Reverts:** a malformed update reverts as a whole. That covers bad magic, a feed count outside
  1 to 8, a wrong length (including bytes after the payload, which Pyth's verifier would ignore),
  a duplicate feed, properties other than price, exponent and feed update time in that order
  (Pyth writes them in the order requested, so `pyth.py` asks in this order), an observation time
  later than its message time, and a message time or any observation time outside the delivery
  window (below). Every feed is checked, including those that will be skipped.
- **Skips:** a feed is skipped, not stored, when its observation is not newer than the stream's, or
  its price is one the bank does not store (a mantissa of 0 or less, or an exponent outside
  [-18, 0]). Its stream keeps its previous report, and the update's other feeds are still stored.
- **Channels:** channels never share storage. Reports on one channel cannot displace another
  channel's.
- **Delivery window:** the message time and every feed's observation time must be from 60 s
  before the block to 6 s after it, both inclusive. A keeper transaction mined more than 60 s
  after the observations reverts. The window binds delivery only: `latest(feed_id, channel)`
  returns the newest report however old it has grown since, so each reader checks the age it
  accepts.
- **Fees:** the value must be exactly `verification_fee()`, or the call reverts. The bank
  cannot take a refund, so an overpayment would otherwise stay with Pyth's verifier.

This is twocrypto-ng's `contracts/main/PythReportBank.vy` without its moving averages. `latest` and
`decode` return `(price, observation_us)`, as there.

## Feeds

| Feed | Id |
|---|---|
| BTC/USD | 1 |
| ETH/USD | 2 |
| WBTC/USD | 103 |
| CBBTC/USD | 397 |
| TBTC/USD | 588 |

All use channel 4 (`fixed_rate@1000ms`). Our API key is refused anything faster.

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
  the delivery window, streams, price scaling, malformed input, fees, decoding and governance.
- **`tests/test_integration.py`:** a real signed BTC/USD + ETH/USD update recorded from Pyth
  (`tests/fixtures/btc_eth_usd.json`), replayed with the block clock set to its message time. The
  stored prices must equal Pyth's own parse. Tampering, a foreign signer, an expired signer and a
  delivery more than 6 s early or 60 s late are refused; one exactly 60 s late is stored. Whenever
  `PYTH_API_KEY` is set, a live test also fetches and delivers one update of both feeds.

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

## Deploy

```sh
uv run python scripts/deploy.py        # dry run on a fork of ETH_RPC_URL
uv run python scripts/deploy.py real   # real deployment
```

Both deploy the bank with Pyth's verifier `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` and read back
its code and verifier. The dry run then delivers a live BTC/USD + ETH/USD update through that
verifier and checks the stored prices against Pyth's own parse, so it also tests the deployed
verifier. The real run signs as the deployer `0xb101b2b0aa02b7167D238B98dc1B0b0404a760E8`, whose key
opens only with a YubiKey touch and then the keystore password (`scripts/yubi_deployers.py`, a
verbatim copy from the yubi-deployers project; it needs `age` and `age-plugin-yubikey`). It moves
ownership to the YieldBasis DAO `0x42F2A41A0D0e65A440813190880c8a65124895Fa` (`CONFIG.owner`), and
verifies the source on Etherscan when `ETHERSCAN_API_KEY` is set. While `CONFIG.bank` holds the
deployed bank's address, both runs check that bank instead of deploying another; set it to `None`
to deploy a new one.

## Submit

```sh
uv run python scripts/submit.py        # dry run on a fork of ETH_RPC_URL
uv run python scripts/submit.py real   # real transaction
```

Fetches one signed update of BTC, WBTC, tBTC, cbBTC and ETH in USD and delivers it to the deployed
bank, signed by the plain key in `WEB3_TESTNET_PK` (delivery needs no privileges). Nothing is sent
when no feed is newer than the bank's report, and every stored report is checked against Pyth's
parse. The first delivery of all five cost 290,712 gas on mainnet; on a fork, later ones take about
95,000 gas of execution, plus about 25,000 for the transaction's base and calldata.
