"""Signed Pyth Lazer updates in the EVM format PythReportBank verifies.

`uv run python pyth.py` fetches the latest BTC/USD and ETH/USD update and saves it
as the integration-test fixture. PYTH_API_KEY comes from the environment or .env.
"""

import json
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

URL = "https://pyth-lazer.dourolabs.app/v1/latest_price"
FEEDS = {"BTC/USD": 1, "ETH/USD": 2}
# Payload channel byte => Pyth channel name. Our API key is entitled to 4 only.
CHANNELS = {1: "real_time", 2: "fixed_rate@50ms", 3: "fixed_rate@200ms", 4: "fixed_rate@1000ms"}
CHANNEL = 4
# Pyth's mainnet signer, trusted by its verifier 0xACeA761c27A909d4D3895128EBe6370FDE2dF481.
SIGNER = "0x26FB61A864c758AE9fBA027a96010480658385B9"
FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "btc_eth_usd.json"


def api_key() -> str | None:
    load_dotenv(Path(__file__).parent / ".env")
    return os.environ.get("PYTH_API_KEY")


def fetch(feed_ids=None, channel=CHANNEL) -> dict:
    """Pyth's response for these feeds (default FEEDS): `evm.data` is the signed
    update in hex, `parsed` its fields."""
    key = api_key()
    if not key:
        raise RuntimeError("Set PYTH_API_KEY in the environment or .env")
    body = {
        "priceFeedIds": list(FEEDS.values() if feed_ids is None else feed_ids),
        # Exactly the properties the bank decodes, in this order: Pyth writes
        # them in the order requested.
        "properties": ["price", "exponent", "feedUpdateTimestamp"],
        "formats": ["evm"],
        "channel": CHANNELS[channel],
        "jsonBinaryEncoding": "hex",
        "parsed": True,
    }
    request = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "report-bank",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def update_bytes(response: dict) -> bytes:
    return bytes.fromhex(response["evm"]["data"])


def reports(response: dict) -> dict:
    """{feed id: (price, observation us)}, the price in 1e18, from Pyth's own parse,
    as the bank decodes it: the price is 0, and the bank skips the feed, unless its
    mantissa is positive and its exponent is in [-18, 0]. Pyth's parse leaves out a
    price it does not have."""
    parsed = {}
    for entry in response["parsed"]["priceFeeds"]:
        price = int(entry.get("price", 0))
        exponent = entry["exponent"]
        if price <= 0 or not -18 <= exponent <= 0:
            price = exponent = 0
        parsed[entry["priceFeedId"]] = (price * 10 ** (18 + exponent), entry["feedUpdateTimestamp"])
    return parsed


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fetch(), indent=2) + "\n")
    print(f"saved {FIXTURE}")
