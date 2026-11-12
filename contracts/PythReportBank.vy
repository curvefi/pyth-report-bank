# pragma version 0.4.3
# pragma optimize gas

"""
@title Pyth Report Bank

@notice Keeps the newest verified Pyth Lazer price of every stream, a stream
        being one feed on one channel, for any number of readers.
Notable features are:
    - Anyone delivers signed Pyth Lazer updates, paying Pyth's verification fee
    - One update carries 1 to MAX_FEEDS feeds on one channel; a batch carries
      up to MAX_BATCH updates, for instance one per channel
    - Streams only move forward: a feed is stored only when its observation is
      newer than the stream's and its price is supported
    - decode() reads one feed of an update without verifying it, for quotes
    - The owner sets the verifier, which decides which prices are authentic

@dev Channels: 1 real_time, 2 fixed_rate@50ms, 3 fixed_rate@200ms,
     4 fixed_rate@1000ms. Channels never share storage.
     Delivery refuses a message from more than MAX_FUTURE_US ahead of the block
     but sets no maximum age: an old observation is stored when it is newer
     than its stream's. latest[feed id][channel] holds the newest report however
     old it is, and each reader enforces its own maximum age.
     Prices have 18 decimals in the feed's quote currency (1e18 = $1 for a USD
     feed). Times are Pyth's, in Unix microseconds.

@license Copyright (c) Curve.Fi, 2026 - all rights reserved

@author curve.fi

@custom:security security@curve.fi
"""

################################################################
#                            MODULES                           #
################################################################

# Import ownership management
from snekmate.auth import ownable

initializes: ownable
exports: (
    ownable.owner,
    ownable.transfer_ownership,
    ownable.renounce_ownership,
)

################################################################
#                           CONSTANTS                          #
################################################################

# Vyper reserves memory for the largest update, so the bounds cost every call
MAX_FEEDS: constant(uint256) = 8  # feeds per update (Pyth allows up to 255)
MAX_BATCH: constant(uint256) = 8  # updates per deliver_batch()

# A Pyth Lazer update in the EVM format is an envelope followed by a payload,
# and the payload is a header followed by its feeds. Byte tables: the envelope
# at decode(), the header and the feeds at _parse_payload().
ENVELOPE_BYTES: constant(uint256) = 71  # 4 magic + 65 signature + 2 payload length
HEADER_BYTES: constant(uint256) = 14  # 4 magic + 8 message time + 1 channel + 1 feed count
FEED_BYTES: constant(uint256) = 27  # 4 feed id + 1 property count + 22 properties
# 22 = (1 id + 8) price + (1 id + 2) exponent + (1 id + 1 presence + 8) feed update time
MAX_PAYLOAD_BYTES: constant(uint256) = HEADER_BYTES + MAX_FEEDS * FEED_BYTES  # 230
MAX_UPDATE_BYTES: constant(uint256) = ENVELOPE_BYTES + MAX_PAYLOAD_BYTES  # 301

EVM_MAGIC: constant(bytes4) = 0x2a22999a  # first 4 bytes of an EVM-format update
PAYLOAD_MAGIC: constant(bytes4) = 0x93c7d375  # first 4 bytes of a payload

# Newest admissible message, ahead of block.timestamp; readers bound the age
MAX_FUTURE_US: constant(uint256) = 6_000_000

################################################################
#                          INTERFACES                          #
################################################################

interface PythLazer:
    def verification_fee() -> uint256: view
    def verifyUpdate(
        update: Bytes[MAX_UPDATE_BYTES],
    ) -> (Bytes[MAX_PAYLOAD_BYTES], address): payable

################################################################
#                            STRUCTS                           #
################################################################

# A stream's newest report
struct Report:
    price: uint256  # 18 decimals in the feed's quote currency
    observation_us: uint256  # when Pyth observed the price


# One feed of an update, decoded
struct Feed:
    feed_id: uint32  # Pyth feed id: 1 BTC/USD, 2 ETH/USD, ...
    price: uint256  # 18 decimals; 0 if unsupported (mantissa <= 0, exponent outside [-18, 0])
    observation_us: uint256  # when Pyth observed the price

################################################################
#                            EVENTS                            #
################################################################

event ReportStored:
    feed_id: indexed(uint32)
    channel: indexed(uint8)
    price: uint256
    observation_us: uint256


event SetVerifier:
    verifier: indexed(address)


################################################################
#                            STORAGE                           #
################################################################

verifier: public(address)  # Pyth Lazer verifier: decides which signed prices are authentic
# feed id => channel => newest report, however old; empty before the first one
latest: public(HashMap[uint32, HashMap[uint8, Report]])

################################################################
#                         CONSTRUCTOR                          #
################################################################

@deploy
def __init__(_verifier: address):
    """
    @notice The deployer becomes the owner
    @param _verifier Pyth Lazer verifier
    """
    ownable.__init__()
    self._set_verifier(_verifier)


################################################################
#                   PERMISSIONLESS FUNCTIONS                   #
################################################################

@external
@payable
@nonreentrant
def deliver_single_report(_update: Bytes[MAX_UPDATE_BYTES]):
    """
    @notice Verify a signed Pyth Lazer update and store each of its feeds that
            is newer than its stream
    @dev Send exactly verification_fee() as value: the bank could not take back
         the verifier's refund of an overpayment.
    @param _update Signed update, as Pyth's API returns it in the "evm" format
    """
    # dev: no point caching the fee: this call warms the verifier and its fee slot,
    # which verifyUpdate reads in the same transaction anyway, while a cached fee
    # would be a cold storage read (measured 1,200 to 1,700 gas more)
    fee: uint256 = staticcall PythLazer(self.verifier).verification_fee()
    assert msg.value == fee, "fee"
    self._deliver(_update, fee)


@external
@payable
@nonreentrant
def deliver_batch(_updates: DynArray[Bytes[MAX_UPDATE_BYTES], MAX_BATCH]):
    """
    @notice deliver_single_report() for each update, in order and all or nothing
    @dev Send exactly verification_fee() per update as value. An empty batch
         reverts, so a keeper's mistake shows in simulation.
    @param _updates Signed updates, for instance one per channel
    """
    assert len(_updates) > 0, "empty batch"
    fee: uint256 = staticcall PythLazer(self.verifier).verification_fee()
    assert msg.value == fee * len(_updates), "fee"
    for i: uint256 in range(len(_updates), bound=MAX_BATCH):
        self._deliver(_updates[i], fee)


################################################################
#                         VIEW FUNCTIONS                       #
################################################################

@view
@external
def verification_fee() -> uint256:
    """
    @notice The verifier's fee per update, in wei
    """
    return staticcall PythLazer(self.verifier).verification_fee()


@view
@external
def decode(_update: Bytes[MAX_UPDATE_BYTES], _feed_id: uint32, _channel: uint8) -> (uint256, uint256):
    """
    @notice One feed of a signed update, for quotes. The signature is not
            verified, and nothing is admitted or stored.
    @dev Reverts where delivery would on structure, and unless the update is on
         `_channel` and carries `_feed_id`. The price is 0 where delivery would
         skip it as unsupported.
         Envelope, from the start of the update; ranges are Python slices, so
         [0:4] is bytes 0 to 3:
             [0:4]      EVM magic          EVM_MAGIC
             [4:36]     signature r        bytes32
             [36:68]    signature s        bytes32
             [68:69]    recovery id        0 or 1; the verifier adds 27
             [69:71]    payload length     n, uint16 big-endian
             [71:71+n]  payload            see _parse_payload(); nothing after
         The signature is over keccak256(payload).
    @param _update Signed update, as for deliver_single_report()
    @param _feed_id Pyth feed id
    @param _channel Pyth channel the update must be on
    @return (price with 18 decimals, 0 if unsupported; observation time)
    """
    # 1. Envelope: the payload fills the rest of the update exactly
    assert convert(slice(_update, 0, 4), bytes4) == EVM_MAGIC, "evm magic"
    payload_length: uint256 = convert(slice(_update, 69, 2), uint256)
    assert len(_update) == ENVELOPE_BYTES + payload_length, "update length"

    # 2. Payload, checked whole
    message_us: uint256 = 0
    channel: uint8 = 0
    feeds: DynArray[Feed, MAX_FEEDS] = []
    message_us, channel, feeds = self._parse_payload(
        # dev: downcast Bytes size; step 1 bounds the payload by MAX_PAYLOAD_BYTES
        convert(slice(_update, ENVELOPE_BYTES, payload_length), Bytes[MAX_PAYLOAD_BYTES])
    )
    assert channel == _channel, "channel"

    # 3. The requested feed
    for feed: Feed in feeds:
        if feed.feed_id == _feed_id:
            return feed.price, feed.observation_us
    raise "feed"


################################################################
#                        OWNER FUNCTIONS                       #
################################################################

@external
def set_verifier(_verifier: address):
    """
    @notice Replace the Pyth Lazer verifier. The stored prices are kept.
    @dev The verifier decides which prices every reader accepts.
    @param _verifier New Pyth Lazer verifier
    """
    ownable._check_owner()
    self._set_verifier(_verifier)


################################################################
#                      INTERNAL FUNCTIONS                      #
################################################################

@internal
def _deliver(_update: Bytes[MAX_UPDATE_BYTES], _fee: uint256):
    """
    @notice Verify an update and store each of its feeds that is newer than
            its stream
    @param _update Signed update
    @param _fee verification_fee(), paid by the caller
    """
    # 1. Verify: Pyth's verifier checks the EVM magic, the signature and the
    #    signer, and returns the payload of the envelope's payload length
    payload: Bytes[MAX_PAYLOAD_BYTES] = b""
    signer: address = empty(address)
    payload, signer = extcall PythLazer(self.verifier).verifyUpdate(_update, value=_fee)
    assert signer != empty(address), "signer"
    # dev: the verifier ignores bytes after the payload; refuse them, as decode() does
    assert len(_update) == ENVELOPE_BYTES + len(payload), "update length"

    # 2. Payload, checked whole; a message from the future reverts
    message_us: uint256 = 0
    channel: uint8 = 0
    feeds: DynArray[Feed, MAX_FEEDS] = []
    message_us, channel, feeds = self._parse_payload(payload)
    assert message_us <= block.timestamp * 1_000_000 + MAX_FUTURE_US, "message time"

    # 3. Store each feed that is supported and newer than its stream
    for feed: Feed in feeds:
        if feed.price == 0:
            continue  # unsupported
        if feed.observation_us <= self.latest[feed.feed_id][channel].observation_us:
            continue  # not newer than the stream's report
        self.latest[feed.feed_id][channel] = Report(price=feed.price, observation_us=feed.observation_us)
        log ReportStored(
            feed_id=feed.feed_id,
            channel=channel,
            price=feed.price,
            observation_us=feed.observation_us,
        )


@internal
@pure
def _parse_payload(_payload: Bytes[MAX_PAYLOAD_BYTES]) -> (uint256, uint8, DynArray[Feed, MAX_FEEDS]):
    """
    @notice Decode a payload and check all of it: the header and every feed
    @dev Byte ranges are Python slices, so [0:4] is bytes 0 to 3. Numbers are
         big-endian, signed ones in two's complement.
         Header, from the start of the payload:
             [0:4]      payload magic      PAYLOAD_MAGIC
             [4:12]     message time       uint64, us
             [12:13]    channel            uint8
             [13:14]    feed count         uint8; 1 to MAX_FEEDS here
             [14:...]   feeds              FEED_BYTES each; nothing after
         Feed k, from its start at HEADER_BYTES + k * FEED_BYTES:
             [0:4]      feed id            uint32
             [4:5]      property count     3
             [5:6]      property id        0 (price)
             [6:14]     price mantissa     int64
             [14:15]    property id        4 (exponent)
             [15:17]    price exponent     int16
             [17:18]    property id        12 (feed update time)
             [18:19]    presence           1
             [19:27]    observation time   uint64, us
         A Pyth subscription chooses its properties. This bank takes exactly
         these three in this order, so every feed is FEED_BYTES long.
    @param _payload Payload of an update, without its envelope
    @return (message time, channel, the feeds in payload order)
    """
    # 1. Header
    assert convert(slice(_payload, 0, 4), bytes4) == PAYLOAD_MAGIC, "payload magic"
    message_us: uint256 = convert(slice(_payload, 4, 8), uint256)
    channel: uint8 = convert(slice(_payload, 12, 1), uint8)
    feed_count: uint256 = convert(slice(_payload, 13, 1), uint256)
    assert feed_count >= 1 and feed_count <= MAX_FEEDS, "feed count"
    assert len(_payload) == HEADER_BYTES + feed_count * FEED_BYTES, "payload length"

    # 2. Feeds: every one is checked, even those delivery will skip
    feeds: DynArray[Feed, MAX_FEEDS] = []
    for k: uint256 in range(feed_count, bound=MAX_FEEDS):
        # dev: slicing the feed out first lets every field below use a fixed offset
        # from the byte table, which compiles cheaper (measured about 400 gas per feed)
        feed: Bytes[FEED_BYTES] = slice(_payload, HEADER_BYTES + k * FEED_BYTES, FEED_BYTES)

        # 2a. Layout: 3 properties, price first; then exponent; then a present time
        assert convert(slice(feed, 4, 2), bytes2) == 0x0300, "price property"
        assert convert(slice(feed, 14, 1), bytes1) == 0x04, "exponent property"
        assert convert(slice(feed, 17, 2), bytes2) == 0x0c01, "observation property"

        # 2b. Feed id, once per update
        feed_id: uint32 = convert(slice(feed, 0, 4), uint32)
        for earlier: Feed in feeds:
            assert earlier.feed_id != feed_id, "duplicate feed"

        # 2c. Observation time, not after the message
        observation_us: uint256 = convert(slice(feed, 19, 8), uint256)
        assert observation_us <= message_us, "observation time"

        # 2d. Price = mantissa * 10**exponent, scaled to 18 decimals
        # dev: converting through bytesN keeps the sign of the two's complement value
        mantissa: int64 = convert(convert(slice(feed, 6, 8), bytes8), int64)
        exponent: int16 = convert(convert(slice(feed, 15, 2), bytes2), int16)
        price: uint256 = 0
        if mantissa > 0 and -18 <= exponent and exponent <= 0:
            price = convert(mantissa, uint256) * 10**convert(18 + exponent, uint256)

        feeds.append(Feed(feed_id=feed_id, price=price, observation_us=observation_us))
    return message_us, channel, feeds


@internal
def _set_verifier(_verifier: address):
    assert _verifier.is_contract, "verifier"
    self.verifier = _verifier
    log SetVerifier(verifier=_verifier)
