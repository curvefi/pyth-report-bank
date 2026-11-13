"""PythReportBank on real signed Pyth Lazer updates for BTC/USD and ETH/USD,
verified by the port of Pyth's verifier trusting Pyth's mainnet signer."""

import json
import time

import boa
import pytest
from conftest import BANK, CHANNEL, FEE, PYTH, deploy

import pyth
from pyth import reports

RECORDED = json.loads(pyth.FIXTURE.read_text())
SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


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
    bank.deliver_report(data, value=FEE)
    assert [log.feed_id for log in bank.get_logs()] == list(expected)
    for feed_id, report in expected.items():
        assert bank.latest(feed_id, CHANNEL) == report
        assert bank.decode(data, feed_id, CHANNEL) == report
    # Delivered again it is not newer: skipped.
    bank.deliver_report(data, value=FEE)
    assert bank.get_logs() == []


def test_recorded_update_is_refused_when_not_authentic(bank):
    data = pyth.update_bytes(RECORDED)
    set_clock(RECORDED)
    # One flipped bit of BTC's price mantissa: the payload no longer matches.
    with boa.reverts("invalid signer"):
        bank.deliver_report(data[:98] + bytes([data[98] ^ 1]) + data[99:], value=FEE)
    # The same signature with s mirrored (high s) recovers Pyth's signer in raw
    # ecrecover, but Pyth's verifier refuses it.
    s = int.from_bytes(data[36:68], "big")
    mirrored = SECP256K1_ORDER - s
    high_s = data[:36] + mirrored.to_bytes(32, "big") + bytes([data[68] ^ 1]) + data[69:]
    with boa.reverts("invalid signature"):
        bank.deliver_report(high_s, value=FEE)
    # So does trusting another signer, or Pyth's signer after its expiry.
    for verifier in (
        PYTH.deploy(boa.env.generate_address(), 2**64, FEE),
        PYTH.deploy(pyth.SIGNER, boa.env.evm.patch.timestamp, FEE),
    ):
        with boa.reverts("invalid signer"):
            BANK.deploy(verifier.address).deliver_report(data, value=FEE)
    for feed_id in pyth.FEEDS.values():
        assert bank.latest(feed_id, CHANNEL) == (0, 0)


def test_recorded_update_is_stored_late_but_not_early(bank):
    data = pyth.update_bytes(RECORDED)
    set_clock(RECORDED)
    # More than 6 s before its message time it is refused.
    boa.env.time_travel(seconds=-8)
    with boa.reverts("message time"):
        bank.deliver_report(data, value=FEE)
    # An hour late it is still stored: readers bound the age.
    boa.env.time_travel(seconds=8 + 3_600)
    bank.deliver_report(data, value=FEE)
    for feed_id, report in reports(RECORDED).items():
        assert bank.latest(feed_id, CHANNEL) == report


@pytest.mark.skipif(not pyth.api_key(), reason="needs PYTH_API_KEY")
def test_live_update_is_stored(bank):
    """One live update of both feeds."""
    live = pyth.fetch()
    boa.env.evm.patch.timestamp = int(time.time())
    bank.deliver_report(pyth.update_bytes(live), value=FEE)
    assert len(bank.get_logs()) == 2
    for feed_id, report in reports(live).items():
        assert bank.latest(feed_id, CHANNEL) == report
