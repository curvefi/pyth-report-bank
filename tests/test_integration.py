"""PythReportBank on real signed Pyth Lazer updates for BTC/USD and ETH/USD,
verified by the port of Pyth's verifier trusting Pyth's mainnet signer."""

import json
import time

import boa
import pytest
from conftest import BANK, CHANNEL, FEE, PYTH, deploy

import pyth

RECORDED = json.loads(pyth.FIXTURE.read_text())
SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def reports(response):
    """{feed id: (price in 1e18, observation us)} from Pyth's own parse."""
    return {
        entry["priceFeedId"]: (
            int(entry["price"]) * 10 ** (18 + entry["exponent"]),
            entry["feedUpdateTimestamp"],
        )
        for entry in response["parsed"]["priceFeeds"]
    }


def set_clock(response):
    """Replay: put the block at the update's message time, rounded up to a second."""
    boa.env.evm.patch.timestamp = -(-int(response["parsed"]["timestampUs"]) // 1_000_000)


@pytest.fixture
def bank():
    return deploy(pyth.SIGNER)


def test_recorded_update_is_stored(bank):
    data = pyth.update_bytes(RECORDED)
    expected = reports(RECORDED)
    assert set(expected) == set(pyth.FEEDS.values())
    set_clock(RECORDED)
    bank.deliver_single_report(data, value=FEE)
    assert [log.feed_id for log in bank.get_logs()] == list(expected)
    for feed_id, report in expected.items():
        assert bank.latest(feed_id, CHANNEL) == report
        assert bank.decode(data, feed_id, CHANNEL) == report
    # Delivered again it is not newer: skipped.
    bank.deliver_single_report(data, value=FEE)
    assert bank.get_logs() == []


def test_recorded_update_is_refused_when_not_authentic(bank):
    data = pyth.update_bytes(RECORDED)
    set_clock(RECORDED)
    # One flipped bit of BTC's price mantissa: the payload no longer matches.
    with boa.reverts("invalid signer"):
        bank.deliver_single_report(data[:98] + bytes([data[98] ^ 1]) + data[99:], value=FEE)
    # The same signature with s mirrored (high s) recovers Pyth's signer in raw
    # ecrecover, but Pyth's verifier refuses it.
    s = int.from_bytes(data[36:68], "big")
    mirrored = SECP256K1_ORDER - s
    high_s = data[:36] + mirrored.to_bytes(32, "big") + bytes([data[68] ^ 1]) + data[69:]
    with boa.reverts("invalid signature"):
        bank.deliver_single_report(high_s, value=FEE)
    # So does trusting another signer, or Pyth's signer after its expiry.
    for verifier in (
        PYTH.deploy(boa.env.generate_address(), 2**64, FEE),
        PYTH.deploy(pyth.SIGNER, boa.env.evm.patch.timestamp, FEE),
    ):
        with boa.reverts("invalid signer"):
            BANK.deploy(verifier.address).deliver_single_report(data, value=FEE)
    for feed_id in pyth.FEEDS.values():
        assert bank.latest(feed_id, CHANNEL) == (0, 0)


def test_recorded_update_delivered_late_is_skipped(bank):
    set_clock(RECORDED)
    boa.env.time_travel(seconds=13)
    bank.deliver_single_report(pyth.update_bytes(RECORDED), value=FEE)
    for feed_id in pyth.FEEDS.values():
        assert bank.latest(feed_id, CHANNEL) == (0, 0)


@pytest.mark.skipif(not pyth.api_key(), reason="needs PYTH_API_KEY")
def test_live_updates_are_stored(bank):
    """One update of both feeds; then each feed signed separately, as a batch
    into a fresh bank."""
    both = pyth.fetch()
    boa.env.evm.patch.timestamp = int(time.time())
    bank.deliver_single_report(pyth.update_bytes(both), value=FEE)
    for feed_id, report in reports(both).items():
        assert bank.latest(feed_id, CHANNEL) == report
    single = [pyth.fetch([feed_id]) for feed_id in pyth.FEEDS.values()]
    boa.env.evm.patch.timestamp = int(time.time())
    fresh = deploy(pyth.SIGNER)
    fresh.deliver_batch([pyth.update_bytes(response) for response in single], value=2 * FEE)
    assert len(fresh.get_logs()) == 2
    for response in single:
        ((feed_id, report),) = reports(response).items()
        assert fresh.latest(feed_id, CHANNEL) == report
