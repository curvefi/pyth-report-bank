# pragma version 0.4.3
"""
@title PythLazer (test port)
@notice verifyUpdate of Pyth's PythLazer.sol 0.3.0 (pyth-crosschain,
        lazer/contracts/evm/src/PythLazer.sol), ported to Vyper so tests verify
        real signatures without a chain fork: the same fee check, envelope
        checks, ECDSA recovery (high-s rejected, as OpenZeppelin's tryRecover),
        signer expiry and refund. Not ported: the proxy, ownership, and signer
        or fee administration; the constructor sets one trusted signer.
"""

MAX_UPDATE_BYTES: constant(uint256) = 301  # PythReportBank's largest update (8 feeds)
EVM_FORMAT_MAGIC: constant(uint256) = 706910618  # 0x2a22999a
# secp256k1 order // 2: OpenZeppelin rejects signatures with a larger s.
HALF_CURVE_ORDER: constant(uint256) = (
    57896044618658097711785492504343953926418782139537452191302581570759080747168
)

verification_fee: public(uint256)
expires_at: public(HashMap[address, uint256])  # signer => unix time it stops being trusted


event RefundFailed:
    payee: indexed(address)
    amount: uint256


@deploy
def __init__(signer: address, expires_at: uint256, fee: uint256):
    self.expires_at[signer] = expires_at
    self.verification_fee = fee


@external
@payable
def verifyUpdate(update: Bytes[MAX_UPDATE_BYTES]) -> (Bytes[MAX_UPDATE_BYTES], address):
    fee: uint256 = self.verification_fee
    assert msg.value >= fee, "Insufficient fee provided"
    assert len(update) >= 71, "input too short"
    assert convert(slice(update, 0, 4), uint256) == EVM_FORMAT_MAGIC, "invalid evm magic"
    payload_len: uint256 = convert(slice(update, 69, 2), uint256)
    assert len(update) >= 71 + payload_len, "input too short"
    payload: Bytes[MAX_UPDATE_BYTES] = slice(update, 71, payload_len)

    s: uint256 = convert(slice(update, 36, 32), uint256)
    signer: address = empty(address)
    if s <= HALF_CURVE_ORDER:
        signer = ecrecover(
            keccak256(payload),
            convert(slice(update, 68, 1), uint256) + 27,
            convert(slice(update, 4, 32), uint256),
            s,
        )
    assert signer != empty(address), "invalid signature"
    assert block.timestamp < self.expires_at[signer], "invalid signer"

    # As Solidity's send: gas 0 plus the 2300 stipend of a call with value. A
    # caller that cannot take the refund is still verified; the excess stays here.
    excess: uint256 = msg.value - fee
    if excess > 0:
        if not raw_call(msg.sender, b"", value=excess, gas=0, revert_on_failure=False):
            log RefundFailed(payee=msg.sender, amount=excess)
    return payload, signer
