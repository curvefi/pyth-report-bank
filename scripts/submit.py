"""Deliver the latest Pyth Lazer reports of BTC, WBTC, tBTC, cbBTC and ETH, in USD, to the
deployed PythReportBank.

    uv run python scripts/submit.py         dry run on a fork of ETH_RPC_URL
    uv run python scripts/submit.py real    real transaction, signed by WEB3_TESTNET_PK

Variables come from the environment or .env; PYTH_API_KEY is required. The sender is a
plain key with a little gas, as delivery needs no privileges. One signed update
carries all five feeds on channel 4 (fixed_rate@1000ms). Nothing is sent when no feed is
newer than the bank's report. After delivery, every stored report is checked against
Pyth's own parse.
"""

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import boa
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import pyth
from scripts.lib import connect, mode, plain_account

CONFIG = SimpleNamespace(
    rpc_url=os.environ.get("ETH_RPC_URL"),
    chain_id=1,
    bank="0x76216954BE01eECdC0D7b00cd8bb72C2e28F0da7",  # PythReportBank, deployed 2026-11-13
    key="WEB3_TESTNET_PK",  # environment variable holding the sender's plain private key
    sender="0x73241E98090042A718f7eb1AF07FAD27ff09A3F3",  # its address; the dry run sends from it
    channel=pyth.CHANNEL,
    feeds={"BTC/USD": 1, "WBTC/USD": 103, "TBTC/USD": 588, "CBBTC/USD": 397, "ETH/USD": 2},
)

BANK = boa.load_partial(str(ROOT / "contracts" / "PythReportBank.vy"))
MAX_FUTURE_US = 6_000_000  # the bank refuses a message further ahead of its block


def newer_reports(bank, cfg, response):
    """{feed id: report} of the feeds the bank will store: supported (pyth.reports gives
    the price 0 otherwise) and observed after the stream's report."""
    reports = pyth.reports(response)
    missing = [name for name, feed_id in cfg.feeds.items() if feed_id not in reports]
    if missing:
        raise RuntimeError(f"Pyth's update lacks {missing}")
    return {
        feed_id: report
        for feed_id, report in reports.items()
        if report[0] > 0 and report[1] > bank.latest(feed_id, cfg.channel)[1]
    }


def wait_for_block(message_us, timeout_s=60):
    """Real run: boa simulates the call on the latest block before sending it, and that
    block can trail Pyth's message by more than the bank admits. Wait for one that
    admits it; the transaction lands in a later block still."""
    deadline = time.time() + timeout_s
    while True:
        block = boa.env._rpc.fetch("eth_getBlockByNumber", ["latest", False])
        if int(block["timestamp"], 16) * 1_000_000 + MAX_FUTURE_US >= message_us:
            return
        if time.time() > deadline:
            raise RuntimeError(f"no block within {timeout_s} s admits the message")
        time.sleep(1)


def read(bank, cfg, feed_id, report, real):
    """The stream's report once it holds `report` or a newer one. A real run retries for
    a few seconds: a load-balanced RPC can answer from a block before our transaction."""
    for _ in range(5 if real else 1):
        stored = bank.latest(feed_id, cfg.channel)
        if stored[1] >= report[1]:
            break
        time.sleep(2)
    return stored


def deliver(bank, cfg, response, newer, real):
    """Deliver `response`, then check every stream: a feed in `newer` holds our report,
    or a newer one another sender stored meanwhile (the bank then skipped ours)."""
    message_us = int(response["parsed"]["timestampUs"])
    if real:
        wait_for_block(message_us)
    else:
        # A fork's clock stands still at its block.
        message_s = -(-message_us // 1_000_000)
        boa.env.evm.patch.timestamp = max(boa.env.evm.patch.timestamp, message_s)
    bank.deliver_report(pyth.update_bytes(response), value=bank.verification_fee())
    for name, feed_id in cfg.feeds.items():
        if feed_id in newer:
            stored = read(bank, cfg, feed_id, newer[feed_id], real)
            if stored == newer[feed_id]:
                state = "stored"
            elif stored[1] > newer[feed_id][1]:
                state = "newer report from another sender"
            else:
                raise RuntimeError(f"{name}: expected {newer[feed_id]}, the bank holds {stored}")
        else:
            stored = bank.latest(feed_id, cfg.channel)
            state = "kept" if stored[1] else "empty"
        price, observation_us = stored
        observed = datetime.fromtimestamp(observation_us / 1e6, UTC).strftime("%H:%M:%S")
        print(f"{name:10s} price {price / 1e18:>12,.2f}  observed {observed} UTC  {state}")


def submit(bank, cfg, response, real):
    """Deliver `response` unless it holds nothing the bank will store. Returns whether a
    transaction was sent."""
    newer = newer_reports(bank, cfg, response)
    if not newer:
        print("nothing the bank would store: not sent")
        return False
    deliver(bank, cfg, response, newer, real)
    return True


def main(args, cfg=CONFIG):
    real = mode(args)
    if not cfg.bank:
        sys.exit("CONFIG.bank is not set: deploy the bank first")
    if not pyth.api_key():
        sys.exit("PYTH_API_KEY is not set")
    connect(cfg.rpc_url, cfg.chain_id, real, cfg.sender, load=lambda: plain_account(cfg.key))
    bank = BANK.at(cfg.bank)
    submit(bank, cfg, pyth.fetch(list(cfg.feeds.values()), cfg.channel), real)
    if not real:
        print("dry run done: nothing was sent")


if __name__ == "__main__":
    main(sys.argv[1:])
