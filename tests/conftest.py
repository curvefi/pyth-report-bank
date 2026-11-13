"""Shared helpers: Pyth Lazer updates built in Python, signed by a test key."""

import struct

import boa
import pytest
from eth_keys import keys
from eth_utils import keccak

WAD = 10**18
BTC, ETH, CHANNEL = 1, 2, 4  # feed ids; channel 4 is fixed_rate@1000ms
FEE = 1  # wei; nonzero so the fee checks run (Pyth's mainnet fee was 0 on 2026-11-12)
FOREVER = 2**64  # signer expiry, unix seconds
PAYLOAD_MAGIC = 0x93C7D375
EVM_MAGIC = bytes.fromhex("2a22999a")
TEST_KEY = keys.PrivateKey(keccak(b"report-bank test signer"))

BANK = boa.load_partial("contracts/PythReportBank.vy")
PYTH = boa.load_partial("tests/mocks/PythLazer.vy")


def now_us():
    return boa.env.evm.patch.timestamp * 1_000_000


def feed(feed_id, price, observation_us=None, exponent=-8):
    """One feed entry; price in 1e18, encoded as a mantissa at `exponent`."""
    mantissa = price // 10 ** (18 + exponent)
    return (feed_id, mantissa, exponent, now_us() if observation_us is None else observation_us)


def payload(feeds, message_us=None, channel=CHANNEL):
    """A Pyth Lazer payload with the properties price, exponent, feed update time."""
    if message_us is None:
        message_us = max(entry[3] for entry in feeds)
    data = struct.pack(">IQBB", PAYLOAD_MAGIC, message_us, channel, len(feeds))
    for feed_id, mantissa, exponent, observation_us in feeds:
        data += struct.pack(
            ">IBBqBhBBQ", feed_id, 3, 0, mantissa, 4, exponent, 12, 1, observation_us
        )
    return data


def sign(data, key=TEST_KEY):
    """Pyth's EVM envelope: magic, signature (r, s, v - 27), payload length, payload."""
    signature = key.sign_msg_hash(keccak(data)).to_bytes()
    return EVM_MAGIC + signature + len(data).to_bytes(2, "big") + data


def update(feeds, **kwargs):
    return sign(payload(feeds, **kwargs))


def deploy(signer, fee=FEE):
    """A bank behind a port of Pyth's verifier that trusts `signer`."""
    boa.env.set_balance(boa.env.eoa, WAD)
    return BANK.deploy(PYTH.deploy(signer, FOREVER, fee).address)


@pytest.fixture
def bank():
    return deploy(TEST_KEY.public_key.to_checksum_address())
