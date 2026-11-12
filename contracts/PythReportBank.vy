# pragma version 0.4.3
# pragma optimize gas
"""
@title PythReportBank
@notice The newest verified Pyth Lazer price of every (feed, channel) stream.
        Anyone delivers signed reports; any number of readers share them.
@dev
     DELIVERY
     deliver_single_report(update) takes one signed Pyth Lazer update of 1 to
     MAX_FEEDS feeds on one channel, with verification_fee() as value.
     deliver_batch(updates) takes up to MAX_BATCH of them, for instance on
     several channels, with verification_fee() per update as value; it stores
     them in order, as that many single deliveries in one call. Each feed must
     carry exactly the properties price, exponent and feed update time, in that
     order, and appear once in its update. A malformed update reverts the whole
     call. A feed is skipped, not stored, when its observation is not newer
     than the stream's, is older than MAX_PAST_US, or has a price this bank
     does not store (mantissa <= 0, exponent outside [-18, 0]). So a delivery
     overtaken by a newer one still succeeds, and still stores its other feeds.

     STREAMS
     A stream is one feed on one channel (1 real_time, 2 fixed_rate@50ms,
     3 fixed_rate@200ms, 4 fixed_rate@1000ms). Channels never share storage:
     reports on one channel cannot displace another channel's.

     FRESHNESS
     The admission windows apply at delivery only. latest() returns the newest
     report however old it is; readers enforce their own maximum age.

     TRUST
     The owner sets the verifier, which decides which prices are authentic for
     every reader. A verifier change keeps the stored prices.

     UNITS
     Prices have 18 decimals in the feed's quote currency (1e18 = $1 for a USD
     feed). Times are Pyth's, in microseconds.
"""
from snekmate.auth import ownable

initializes: ownable
exports: (ownable.owner, ownable.transfer_ownership, ownable.renounce_ownership)


MAX_FEEDS: constant(uint256) = 16  # per signed update
MAX_BATCH: constant(uint256) = 8  # signed updates per deliver_batch()
# Pyth's EVM envelope: magic (4 bytes), signature (65), payload length (2), payload.
ENVELOPE_BYTES: constant(uint256) = 71
HEADER_BYTES: constant(uint256) = 14  # payload magic, message time, channel, feed count
FEED_BYTES: constant(uint256) = 27  # feed id and its three properties
MAX_PAYLOAD_BYTES: constant(uint256) = HEADER_BYTES + MAX_FEEDS * FEED_BYTES
MAX_UPDATE_BYTES: constant(uint256) = ENVELOPE_BYTES + MAX_PAYLOAD_BYTES
PYTH_PAYLOAD_MAGIC: constant(uint256) = 2479346549  # 0x93c7d375: Pyth Lazer payload format.
# Admission windows around block.timestamp.
MAX_PAST_US: constant(uint256) = 12_000_000  # oldest admissible observation
MAX_FUTURE_US: constant(uint256) = 6_000_000  # newest admissible message


interface PythLazer:
    def verification_fee() -> uint256: view
    def verifyUpdate(
        update: Bytes[MAX_UPDATE_BYTES],
    ) -> (Bytes[MAX_PAYLOAD_BYTES], address): payable


verifier: public(address)  # Pyth Lazer verifier: it decides which signed prices are authentic.
# feed id => channel => price << 64 | observation_us; 0 before the first report.
packed_reports: HashMap[uint32, HashMap[uint8, uint256]]


event ReportStored:
    feed_id: indexed(uint32)
    channel: indexed(uint8)
    price: uint256
    observation_us: uint256


event SetVerifier:
    verifier: address


@deploy
def __init__(verifier: address):
    """
    @param verifier Pyth Lazer verifier; the deployer becomes the owner
    """
    ownable.__init__()
    self._set_verifier(verifier)


# ---------------------------------- Delivery -----------------------------------


@external
@payable
@nonreentrant
def deliver_single_report(update: Bytes[MAX_UPDATE_BYTES]):
    """
    @notice Verify a signed Pyth Lazer update and store each of its feeds that
            is newer than its stream; send verification_fee() as value.
    """
    self._deliver(update, msg.value)


@external
@payable
@nonreentrant
def deliver_batch(updates: DynArray[Bytes[MAX_UPDATE_BYTES], MAX_BATCH]):
    """
    @notice deliver_single_report() for each update, in order and all or
            nothing; send verification_fee() times their number as value.
    """
    fee: uint256 = staticcall PythLazer(self.verifier).verification_fee()
    assert msg.value == fee * len(updates), "fee"
    for i: uint256 in range(len(updates), bound=MAX_BATCH):
        self._deliver(updates[i], fee)


@internal
def _deliver(update: Bytes[MAX_UPDATE_BYTES], fee: uint256):
    """@dev Verify `update`, paying `fee`, and store each feed newer than its stream."""
    payload: Bytes[MAX_PAYLOAD_BYTES] = b""
    signer: address = empty(address)
    payload, signer = extcall PythLazer(self.verifier).verifyUpdate(update, value=fee)
    assert signer != empty(address), "signer"

    message_us: uint256 = 0
    channel: uint8 = 0
    feed_count: uint256 = 0
    message_us, channel, feed_count = self._decode_header(
        slice(payload, 0, HEADER_BYTES), len(payload)
    )
    block_time_us: uint256 = block.timestamp * 1_000_000
    assert message_us <= block_time_us + MAX_FUTURE_US, "message time"

    feed_ids: DynArray[uint32, MAX_FEEDS] = []
    for k: uint256 in range(feed_count, bound=MAX_FEEDS):
        feed_id: uint32 = 0
        price: uint256 = 0
        observation_us: uint256 = 0
        feed_id, price, observation_us = self._decode_feed(
            slice(payload, HEADER_BYTES + k * FEED_BYTES, FEED_BYTES)
        )
        assert feed_id not in feed_ids, "duplicate feed"
        feed_ids.append(feed_id)
        assert observation_us <= message_us, "observation time"
        if price == 0 or observation_us + MAX_PAST_US < block_time_us:
            continue  # an unsupported price, or too old
        if observation_us <= self.packed_reports[feed_id][channel] % 2**64:
            continue  # not newer than the stream's report
        # price < 2**123: a positive int64 mantissa times at most 10**18 (exponent <= 0).
        self.packed_reports[feed_id][channel] = (price << 64) | observation_us
        log ReportStored(feed_id=feed_id, channel=channel, price=price, observation_us=observation_us)


# ----------------------------------- Views -------------------------------------


@external
@view
def latest(feed_id: uint32, channel: uint8) -> (uint256, uint256):
    """
    @return (price, observation_us) of the stream's newest report; (0, 0) before
            the first one
    """
    packed: uint256 = self.packed_reports[feed_id][channel]
    return packed >> 64, packed % 2**64


@external
@view
def verification_fee() -> uint256:
    return staticcall PythLazer(self.verifier).verification_fee()


@external
@view
def decode(update: Bytes[MAX_UPDATE_BYTES], feed_id: uint32, channel: uint8) -> (uint256, uint256):
    """
    @notice (price, observation_us) of one feed in a signed update, for quotes:
            the signature is not verified and nothing is admitted or stored.
    @dev Reverts where delivery would on structure (duplicate feeds and an
         observation after its message included), and unless the update is on
         `channel` and carries `feed_id`. The price is 0 where delivery would
         skip it as unsupported.
    """
    payload_bytes: uint256 = convert(slice(update, ENVELOPE_BYTES - 2, 2), uint256)
    assert len(update) == ENVELOPE_BYTES + payload_bytes, "update length"
    message_us: uint256 = 0
    update_channel: uint8 = 0
    feed_count: uint256 = 0
    message_us, update_channel, feed_count = self._decode_header(
        slice(update, ENVELOPE_BYTES, HEADER_BYTES), payload_bytes
    )
    assert update_channel == channel, "channel"

    feed_ids: DynArray[uint32, MAX_FEEDS] = []
    price: uint256 = 0
    observation_us: uint256 = 0
    for k: uint256 in range(feed_count, bound=MAX_FEEDS):
        entry_id: uint32 = 0
        entry_price: uint256 = 0
        entry_observation_us: uint256 = 0
        entry_id, entry_price, entry_observation_us = self._decode_feed(
            slice(update, ENVELOPE_BYTES + HEADER_BYTES + k * FEED_BYTES, FEED_BYTES)
        )
        assert entry_id not in feed_ids, "duplicate feed"
        feed_ids.append(entry_id)
        assert entry_observation_us <= message_us, "observation time"
        if entry_id == feed_id:
            price = entry_price
            observation_us = entry_observation_us
    assert feed_id in feed_ids, "feed"
    return price, observation_us


# ---------------------------------- Decoding -----------------------------------


@internal
@pure
def _decode_header(header: Bytes[HEADER_BYTES], payload_bytes: uint256) -> (uint256, uint8, uint256):
    """
    @dev A payload's header; the whole payload, `payload_bytes` long, must hold
         exactly its feeds. Big-endian byte ranges:
             [0:4]    magic                PYTH_PAYLOAD_MAGIC
             [4:12]   message time, us
             [12]     channel
             [13]     feed count           1 to MAX_FEEDS
    @return (message time us, channel, feed count)
    """
    assert convert(slice(header, 0, 4), uint256) == PYTH_PAYLOAD_MAGIC, "payload magic"
    feed_count: uint256 = convert(slice(header, 13, 1), uint256)
    assert feed_count >= 1 and feed_count <= MAX_FEEDS, "feed count"
    assert payload_bytes == HEADER_BYTES + feed_count * FEED_BYTES, "payload length"
    return convert(slice(header, 4, 8), uint256), convert(slice(header, 12, 1), uint8), feed_count


@internal
@pure
def _decode_feed(feed: Bytes[FEED_BYTES]) -> (uint32, uint256, uint256):
    """
    @dev One feed. Big-endian byte ranges:
             [0:4]    feed id
             [4]      property count       3
             [5]      property id          0 (price)
             [6:14]   price mantissa       int64
             [14]     property id          4 (exponent)
             [15:17]  price exponent       int16
             [17]     property id          12 (feed update time)
             [18]     value present        1
             [19:27]  observation time, us
    @return (feed id, price with 18 decimals or 0 if unsupported, observation time us)
    """
    assert slice(feed, 4, 2) == b"\x03\x00", "price property"
    assert slice(feed, 14, 1) == b"\x04", "exponent property"
    assert slice(feed, 17, 2) == b"\x0c\x01", "observation property"

    mantissa: int64 = convert(convert(slice(feed, 6, 8), bytes8), int64)
    exponent: int16 = convert(convert(slice(feed, 15, 2), bytes2), int16)
    price: uint256 = 0
    if mantissa > 0 and -18 <= exponent and exponent <= 0:
        price = convert(mantissa, uint256) * 10**convert(18 + exponent, uint256)
    return convert(slice(feed, 0, 4), uint32), price, convert(slice(feed, 19, 8), uint256)


# --------------------------------- Governance ----------------------------------


@external
def set_verifier(verifier: address):
    """
    @notice Replace the Pyth Lazer verifier. Owner only.
    @dev The verifier decides which prices every reader accepts.
    """
    ownable._check_owner()
    self._set_verifier(verifier)


@internal
def _set_verifier(verifier: address):
    assert verifier.is_contract, "verifier"
    self.verifier = verifier
    log SetVerifier(verifier=verifier)
