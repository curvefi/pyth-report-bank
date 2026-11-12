"""scripts/submit.py against the port of Pyth's verifier: what it sends and checks."""

from types import SimpleNamespace

import pytest
from conftest import BTC, CHANNEL, ETH, FEE, WAD, feed, now_us, payload, sign, update

import pyth
from scripts import submit as script

CFG = SimpleNamespace(**{**vars(script.CONFIG), "feeds": {"BTC/USD": BTC, "ETH/USD": ETH}})


def response(feeds):
    """Pyth's response shape for a test-signed update of `feeds`; a 0 EMA mantissa is
    left out of the parse, as Pyth does."""
    data = payload(feeds)
    parsed = []
    for feed_id, mantissa, exponent, observation_us, ema in feeds:
        entry = {"priceFeedId": feed_id, "price": str(mantissa), "exponent": exponent}
        entry["feedUpdateTimestamp"] = observation_us
        if ema:
            entry["emaPrice"] = str(ema)
        parsed.append(entry)
    message_us = max(entry[3] for entry in feeds)
    return {
        "evm": {"data": sign(data).hex()},
        "parsed": {"timestampUs": str(message_us), "priceFeeds": parsed},
    }


def test_submit_stores_then_sends_nothing_older(bank, capsys):
    # ETH without an EMA price: the bank keeps its report, BTC is still stored.
    update = response([feed(BTC, 70_000 * WAD), feed(ETH, 2_000 * WAD, ema_price=0)])
    assert script.submit(bank, CFG, update, real=False)
    assert bank.latest(BTC, CHANNEL) == pyth.reports(update)[BTC]
    assert bank.latest(ETH, CHANNEL) == (0, 0, 0)
    assert "empty" in capsys.readouterr().out  # ETH's stream was never written
    # The same update again holds nothing newer: no transaction.
    assert not script.submit(bank, CFG, update, real=False)
    assert bank.get_logs() == []


def test_submit_sends_nothing_the_bank_would_skip(bank):
    # Supported except for the exponent, which the bank refuses outside [-18, 0].
    update = response([(BTC, 7, 1, now_us(), 7), (ETH, 2, 1, now_us(), 2)])
    assert script.newer_reports(bank, CFG, update) == {}
    assert not script.submit(bank, CFG, update, real=False)


def test_a_newer_report_from_another_sender_is_not_a_failure(bank, capsys):
    ours = response([feed(BTC, 70_000 * WAD), feed(ETH, 2_000 * WAD)])
    newer = script.newer_reports(bank, CFG, ours)
    # Another sender stores later observations while ours is pending; the bank skips ours.
    later = now_us() + 1_000_000
    bank.deliver_single_report(
        update([feed(BTC, 71_000 * WAD, later)], message_us=later), value=FEE
    )
    script.deliver(bank, CFG, ours, newer, real=False)
    out = capsys.readouterr().out
    assert "newer report from another sender" in out
    assert bank.latest(BTC, CHANNEL)[0] == 71_000 * WAD
    assert bank.latest(ETH, CHANNEL) == pyth.reports(ours)[ETH]


def test_submit_refuses_an_update_missing_a_feed(bank):
    with pytest.raises(RuntimeError, match="lacks"):
        script.submit(bank, CFG, response([feed(BTC, 70_000 * WAD)]), real=False)
