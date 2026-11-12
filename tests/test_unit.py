"""PythReportBank on updates built and signed in the test: delivery, the
future-message check, streams, scaling, malformed input, batches, fees, decoding
and governance."""

import boa
from conftest import (
    BANK,
    BTC,
    CHANNEL,
    ETH,
    FEE,
    FOREVER,
    PYTH,
    TEST_KEY,
    WAD,
    deploy,
    feed,
    now_us,
    payload,
    sign,
    update,
)
from eth_keys import keys


def deliver(bank, *feeds, value=FEE, **kwargs):
    """Deliver an update of these feeds; return its logs."""
    bank.deliver_single_report(update(list(feeds), **kwargs), value=value)
    return bank.get_logs()


# --------------------------------- Delivery ----------------------------------


def test_a_stream_advances_only_on_newer_observations(bank):
    t = now_us()
    assert bank.latest(BTC, CHANNEL) == (0, 0)
    (log,) = deliver(bank, feed(BTC, 70_000 * WAD, t - 1_000_000), message_us=t)
    assert bank.latest(BTC, CHANNEL) == (70_000 * WAD, t - 1_000_000)
    assert (log.feed_id, log.channel, log.price, log.observation_us) == (
        BTC,
        CHANNEL,
        70_000 * WAD,
        t - 1_000_000,
    )
    # Equal or older observations are skipped without reverting, even in a
    # later message.
    for observation in (t - 1_000_000, t - 2_000_000):
        assert deliver(bank, feed(BTC, 71_000 * WAD, observation), message_us=t) == []
        assert bank.latest(BTC, CHANNEL) == (70_000 * WAD, t - 1_000_000)
    # Only the observation counts: a newer one is stored even in a message older
    # than the accepted one's.
    deliver(bank, feed(BTC, 71_000 * WAD, t - 500_000))
    assert bank.latest(BTC, CHANNEL) == (71_000 * WAD, t - 500_000)


def test_streams_are_separate_per_feed_and_channel(bank):
    t = now_us()
    deliver(bank, feed(BTC, 70_000 * WAD, t))
    # An older observation on another channel starts that channel's own stream.
    deliver(bank, feed(BTC, 90_000 * WAD, t - 1_000_000), channel=1)
    deliver(bank, feed(ETH, 3_000 * WAD, t - 1_000_000))
    assert bank.latest(BTC, CHANNEL) == (70_000 * WAD, t)
    assert bank.latest(BTC, 1) == (90_000 * WAD, t - 1_000_000)
    assert bank.latest(ETH, CHANNEL) == (3_000 * WAD, t - 1_000_000)
    assert bank.latest(ETH, 1) == (0, 0)


def test_each_feed_is_stored_or_skipped_on_its_own(bank):
    t = now_us()
    deliver(bank, feed(ETH, 3_000 * WAD, t), feed(103, WAD, t - 1_000_000))
    with boa.env.anchor():
        boa.env.time_travel(seconds=1)
        t1 = now_us()
        logs = deliver(
            bank,
            feed(ETH, 3_100 * WAD, t),  # not newer
            (103, 0, -8, t1),  # zero price
            feed(BTC, 70_000 * WAD, t1),  # stored
            (397, -5, -8, t1),  # negative price
            (588, 7, 1, t1),  # exponent above 0
            (631, 7, -19, t1),  # exponent below -18
            feed(2_000, WAD, t1 - 3_600_000_000),  # an hour old, but new to its stream: stored
        )
        assert [log.feed_id for log in logs] == [BTC, 2_000]
        assert bank.latest(BTC, CHANNEL) == (70_000 * WAD, t1)
        assert bank.latest(2_000, CHANNEL) == (WAD, t1 - 3_600_000_000)
        # Skipped feeds keep their stored reports.
        assert bank.latest(ETH, CHANNEL) == (3_000 * WAD, t)
        assert bank.latest(103, CHANNEL) == (WAD, t - 1_000_000)
        for skipped in (397, 588, 631):
            assert bank.latest(skipped, CHANNEL) == (0, 0)
        # A delivery with nothing new succeeds and stores nothing.
        assert deliver(bank, feed(ETH, 3_200 * WAD, t)) == []
    # Eight feeds fit one update, and each lands in its own stream; nine do not decode.
    eight = update([feed(1_000 + k, (k + 1) * WAD, t - k) for k in range(8)], message_us=t)
    bank.deliver_single_report(eight, value=FEE)
    for k in range(8):
        assert bank.latest(1_000 + k, CHANNEL) == ((k + 1) * WAD, t - k)
        assert bank.decode(eight, 1_000 + k, CHANNEL) == ((k + 1) * WAD, t - k)
    with boa.reverts():
        deliver(bank, *[feed(2_000 + k, WAD, t) for k in range(9)])


def test_prices_scale_to_18_decimals(bank):
    t = now_us()
    deliver(
        bank,
        (BTC, 7_385_842_700_888, -8, t),
        (ETH, 1, -18, t),
        (103, 1, 0, t),
        (397, 2**63 - 1, 0, t),  # the largest mantissa
    )
    assert bank.latest(BTC, CHANNEL) == (73_858_427_008_880_000_000_000, t)
    assert bank.latest(ETH, CHANNEL) == (1, t)
    assert bank.latest(103, CHANNEL) == (WAD, t)
    assert bank.latest(397, CHANNEL) == ((2**63 - 1) * WAD, t)


def test_only_messages_from_the_future_are_refused(bank):
    t = now_us()
    # No maximum age: any observation newer than its stream's is stored.
    deliver(bank, feed(BTC, WAD, t - 86_400_000_000), message_us=t)
    assert bank.latest(BTC, CHANNEL) == (WAD, t - 86_400_000_000)
    deliver(bank, feed(BTC, 2 * WAD, t - 12_000_000), message_us=t)
    assert bank.latest(BTC, CHANNEL) == (2 * WAD, t - 12_000_000)
    # A message up to 6 s ahead of the block is admitted, its observations too.
    deliver(bank, feed(ETH, WAD, t + 6_000_000))
    assert bank.latest(ETH, CHANNEL) == (WAD, t + 6_000_000)
    with boa.reverts("message time"):
        bank.deliver_single_report(update([feed(103, WAD, t)], message_us=t + 6_000_001), value=FEE)
    # An observation may equal its message time but not pass it.
    with boa.reverts("observation time"):
        bank.deliver_single_report(update([feed(103, WAD, t)], message_us=t - 1), value=FEE)
    # A stored report never expires.
    boa.env.time_travel(seconds=86_400)
    assert bank.latest(BTC, CHANNEL) == (2 * WAD, t - 12_000_000)


def test_malformed_updates_revert_as_a_whole(bank):
    t = now_us()
    good = payload([feed(BTC, 70_000 * WAD, t)])
    entry = good[14:]
    for data, reason in (
        (b"\x00" + good[1:], "payload magic"),
        (good[:13] + b"\x00", "feed count"),
        (good[:13] + b"\x09" + entry, "feed count"),
        (good[:13] + b"\x02" + entry, "payload length"),
        (good + b"\x00", "payload length"),
        (good[:18] + b"\x04" + good[19:], "price property"),
        (good[:28] + b"\x05" + good[29:], "exponent property"),
        (good[:32] + b"\x00" + good[33:], "observation property"),
        (payload([feed(BTC, WAD, t), feed(BTC, WAD, t - 1)]), "duplicate feed"),
    ):
        with boa.reverts(reason):
            bank.deliver_single_report(sign(data), value=FEE)
    # A valid first feed is not stored when a later one is malformed, even one
    # that would be skipped.
    for second, reason in (
        (feed(ETH, WAD, t), "duplicate feed"),
        ((BTC, 0, -8, t + 1), "observation time"),  # zero price, after its message
    ):
        with boa.reverts(reason):
            bank.deliver_single_report(update([feed(ETH, WAD, t), second], message_us=t), value=FEE)
    # Nothing may follow the payload, though Pyth's verifier would ignore it.
    with boa.reverts("update length"):
        bank.deliver_single_report(sign(good) + b"\x00", value=FEE)
    assert bank.latest(ETH, CHANNEL) == (0, 0)
    assert bank.latest(BTC, CHANNEL) == (0, 0)


def test_unverified_updates_store_nothing(bank):
    t = now_us()
    good = update([feed(BTC, 70_000 * WAD, t)])
    stranger = keys.PrivateKey(b"\x07" * 32)
    for data, value, reason in (
        (sign(payload([feed(BTC, WAD, t)]), stranger), FEE, "invalid signer"),
        (good[:-1] + bytes([good[-1] ^ 1]), FEE, "invalid signer"),  # tampered payload
        (b"\x00" + good[1:], FEE, "invalid evm magic"),
        # Exactly the fee: the bank could not take back a refund of the excess.
        (good, FEE - 1, "fee"),
        (good, FEE + 1, "fee"),
    ):
        with boa.reverts(reason):
            bank.deliver_single_report(data, value=value)
    assert bank.latest(BTC, CHANNEL) == (0, 0)
    assert boa.env.get_balance(bank.verifier()) == 0
    # A verifier that names no signer is refused too.
    signerless = boa.loads(
        f"@external\n@view\ndef verification_fee() -> uint256:\n    return {FEE}\n"
        "@external\n@payable\ndef verifyUpdate(u: Bytes[301]) -> (Bytes[301], address):\n"
        "    return slice(u, 71, len(u) - 71), empty(address)"
    )
    bank.set_verifier(signerless.address)
    with boa.reverts("signer"):
        bank.deliver_single_report(good, value=FEE)


# ----------------------------------- Batch -----------------------------------


def test_a_batch_delivers_its_updates_in_order(bank):
    t = now_us()
    bank.deliver_batch(
        [
            update([feed(BTC, 70_000 * WAD, t - 1_000_000), feed(ETH, 3_000 * WAD, t)]),
            update([feed(BTC, 90_000 * WAD, t - 2_000_000)], channel=1),  # its own stream
            update([feed(BTC, 71_000 * WAD, t)]),  # newer than the first: stored
            update([feed(BTC, 72_000 * WAD, t - 500_000)]),  # older than the third: skipped
            update([feed(BTC, 73_000 * WAD, t)]),  # as old as the third: skipped
        ],
        value=5 * FEE,
    )
    logs = [(log.feed_id, log.channel, log.price) for log in bank.get_logs()]
    assert logs == [
        (BTC, CHANNEL, 70_000 * WAD),
        (ETH, CHANNEL, 3_000 * WAD),
        (BTC, 1, 90_000 * WAD),
        (BTC, CHANNEL, 71_000 * WAD),
    ]
    assert bank.latest(BTC, CHANNEL) == (71_000 * WAD, t)
    assert bank.latest(BTC, 1) == (90_000 * WAD, t - 2_000_000)
    assert bank.latest(ETH, CHANNEL) == (3_000 * WAD, t)
    # Every update is verified and paid for, skipped or not; the bank keeps nothing.
    assert boa.env.get_balance(bank.verifier()) == 5 * FEE
    assert boa.env.get_balance(bank.address) == 0
    # Eight updates fit one batch, each stored in its own stream.
    batch = [update([feed(1_000 + k, (k + 1) * WAD, t - k)], message_us=t) for k in range(8)]
    bank.deliver_batch(batch, value=8 * FEE)
    for k in range(8):
        assert bank.latest(1_000 + k, CHANNEL) == ((k + 1) * WAD, t - k)
    # An empty batch reverts, whatever its value.
    for value in (0, FEE):
        with boa.reverts("empty batch"):
            bank.deliver_batch([], value=value)


def test_a_free_verifier_takes_no_value():
    bank = deploy(TEST_KEY.public_key.to_checksum_address(), fee=0)
    t = now_us()
    single = update([feed(103, WAD, t)])
    with boa.reverts("fee"):
        bank.deliver_single_report(single, value=1)
    bank.deliver_single_report(single, value=0)
    assert bank.latest(103, CHANNEL) == (WAD, t)
    batch = [update([feed(BTC, WAD, t)]), update([feed(ETH, WAD, t)])]
    with boa.reverts("fee"):
        bank.deliver_batch(batch, value=1)
    bank.deliver_batch(batch, value=0)
    assert bank.latest(ETH, CHANNEL) == (WAD, t)


def test_a_batch_is_all_or_nothing(bank):
    t = now_us()
    deliver(bank, feed(BTC, 60_000 * WAD, t - 1))
    good = update([feed(BTC, 70_000 * WAD, t)])
    for batch, value, reason in (
        ([good, good], 2 * FEE - 1, "fee"),
        ([good, good], 2 * FEE + 1, "fee"),
        ([good, update([feed(ETH, WAD, t), feed(ETH, WAD, t)])], 2 * FEE, "duplicate feed"),
        (
            [good, sign(payload([feed(ETH, WAD, t)]), keys.PrivateKey(b"\x07" * 32))],
            2 * FEE,
            "invalid signer",
        ),
    ):
        with boa.reverts(reason):
            bank.deliver_batch(batch, value=value)
    with boa.reverts():  # more than eight updates do not decode
        bank.deliver_batch([good] * 9, value=9 * FEE)
    # Nothing stored, nothing paid: the earlier report and the one fee for it stay.
    assert bank.latest(BTC, CHANNEL) == (60_000 * WAD, t - 1)
    assert boa.env.get_balance(bank.verifier()) == FEE
    assert bank.latest(ETH, CHANNEL) == (0, 0)


# --------------------------------- Decoding ----------------------------------


def test_decode_reads_one_feed_without_verifying(bank):
    t = now_us()
    data = update([feed(BTC, 70_000 * WAD, t), feed(ETH, 3_000 * WAD, t - 1), (103, 0, -8, t)])
    unsigned = data[:4] + bytes(65) + data[69:]
    assert bank.decode(unsigned, ETH, CHANNEL) == (3_000 * WAD, t - 1)
    assert bank.decode(data, BTC, CHANNEL) == (70_000 * WAD, t)
    assert bank.decode(data, 103, CHANNEL) == (0, t)  # unsupported price: 0
    # Old reports decode too: nothing is admitted.
    old = update([feed(BTC, WAD, t - 3_600_000_000)])
    assert bank.decode(old, BTC, CHANNEL) == (WAD, t - 3_600_000_000)
    for args, reason in (
        ((data, 397, CHANNEL), "feed"),
        ((data, BTC, 1), "channel"),
        ((update([feed(BTC, WAD, t), feed(BTC, WAD, t)]), BTC, CHANNEL), "duplicate feed"),
        ((update([feed(BTC, WAD, t)], message_us=t - 1), BTC, CHANNEL), "observation time"),
        ((b"\x00" + data[1:], BTC, CHANNEL), "evm magic"),
        ((data + b"\x00", BTC, CHANNEL), "update length"),
        (
            (data[:69] + (len(data) - 72).to_bytes(2, "big") + data[71:-1], BTC, CHANNEL),
            "payload length",
        ),
    ):
        with boa.reverts(reason):
            bank.decode(*args)
    assert bank.latest(BTC, CHANNEL) == (0, 0)


# -------------------------------- Governance ---------------------------------


def test_owner_sets_the_verifier(bank):
    t = now_us()
    deliver(bank, feed(BTC, 70_000 * WAD, t))
    assert bank.verification_fee() == FEE
    other_key = keys.PrivateKey(b"\x09" * 32)
    other = PYTH.deploy(other_key.public_key.to_checksum_address(), FOREVER, 2)
    stranger = boa.env.generate_address()
    with boa.reverts("ownable: caller is not the owner"):
        bank.set_verifier(other.address, sender=stranger)
    for bad in ("0x0000000000000000000000000000000000000000", boa.env.generate_address()):
        with boa.reverts("verifier"):
            bank.set_verifier(bad)
    bank.set_verifier(other.address)
    assert bank.get_logs()[-1].verifier == other.address
    assert (bank.verifier(), bank.verification_fee()) == (other.address, 2)
    # Stored prices stay; deliveries now need the new verifier's signer and fee.
    assert bank.latest(BTC, CHANNEL) == (70_000 * WAD, t)
    boa.env.time_travel(seconds=1)
    with boa.reverts("invalid signer"):
        deliver(bank, feed(BTC, 71_000 * WAD), value=2)
    bank.deliver_single_report(sign(payload([feed(BTC, 71_000 * WAD)]), other_key), value=2)
    assert bank.latest(BTC, CHANNEL)[0] == 71_000 * WAD
    dao = boa.env.generate_address("dao")
    bank.transfer_ownership(dao)
    with boa.reverts("ownable: caller is not the owner"):
        bank.set_verifier(other.address)
    bank.set_verifier(other.address, sender=dao)


def test_the_fee_is_read_live_from_the_verifier(bank):
    t = now_us()
    verifier = PYTH.at(bank.verifier())
    # Pyth raises its fee: the old one is refused, the new one accepted at once.
    verifier.eval("self.verification_fee = 3")
    assert bank.verification_fee() == 3
    with boa.reverts("fee"):
        deliver(bank, feed(BTC, WAD, t), value=FEE)
    deliver(bank, feed(BTC, WAD, t), value=3)
    # Pyth drops it to zero: any value is refused.
    verifier.eval("self.verification_fee = 0")
    with boa.reverts("fee"):
        deliver(bank, feed(ETH, WAD, t), value=3)
    deliver(bank, feed(ETH, WAD, t), value=0)
    assert bank.latest(ETH, CHANNEL) == (WAD, t)


def test_constructor_checks_the_verifier():
    with boa.reverts("verifier"):
        BANK.deploy(boa.env.generate_address())
    verifier = PYTH.deploy(TEST_KEY.public_key.to_checksum_address(), FOREVER, FEE)
    bank = BANK.deploy(verifier.address)
    assert (bank.verifier(), bank.owner()) == (verifier.address, boa.env.eoa)
