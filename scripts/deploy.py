"""Deploy PythReportBank on Ethereum, against Pyth's Lazer verifier.

    uv run python scripts/deploy.py         dry run on a fork of ETH_RPC_URL
    uv run python scripts/deploy.py real    real deployment: YubiKey touch, then keystore password

Variables come from the environment or .env. Both runs deploy the bank and read back its
code and every parameter. The dry run then delivers a live BTC/USD + ETH/USD update
through Pyth's deployed verifier (when PYTH_API_KEY is set) and checks the stored prices
against Pyth's own parse. The real run moves ownership to CONFIG.owner if it is set, and
verifies the source on Etherscan when ETHERSCAN_API_KEY is set. To resume an interrupted
real run without redeploying, set CONFIG.bank to the address it printed.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import boa
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import pyth
from scripts.lib import connect, eq, mode, yubi_account

CONFIG = SimpleNamespace(
    rpc_url=os.environ.get("ETH_RPC_URL"),
    chain_id=1,
    verifier="0xACeA761c27A909d4D3895128EBe6370FDE2dF481",  # Pyth Lazer verifier
    # The YubiKey-held key ~/.config/eth/<deployer>.json.age signs; the dry run deploys from it.
    deployer="0xb101b2b0aa02b7167D238B98dc1B0b0404a760E8",
    owner="0x42F2A41A0D0e65A440813190880c8a65124895Fa",  # YieldBasis DAO; None keeps the deployer.
    bank=None,  # Resume: the address of an earlier deployment.
)

BANK = boa.load_partial(str(ROOT / "contracts" / "PythReportBank.vy"))


def check(bank, cfg):
    """Raise unless the deployed code and the verifier match the source and CONFIG."""
    runtime = bank.bytecode
    if bank.data_section_size:
        runtime = runtime[: -bank.data_section_size]
    if runtime != BANK.compiler_data.bytecode_runtime:
        raise RuntimeError("Onchain runtime bytecode does not match contracts/PythReportBank.vy")
    if not eq(bank.verifier(), cfg.verifier):
        raise RuntimeError(f"verifier is {bank.verifier()}, expected {cfg.verifier}")
    print(f"verifier {bank.verifier()}, fee {bank.verification_fee()} wei, owner {bank.owner()}")


def deploy(cfg):
    """Deploy or resume, check, and move ownership to cfg.owner if it is set. The bank
    must end up owned by cfg.owner, or by the sender when cfg.owner is None."""
    if cfg.bank:
        bank = BANK.at(cfg.bank)
        # Etherscan verification needs the constructor arguments of the deployment.
        bank.ctor_calldata = bank._ctor.prepare_calldata(cfg.verifier)
        print(f"reusing bank {bank.address}")
    else:
        bank = BANK.deploy(cfg.verifier)
        print(f"bank deployed: {bank.address} (CONFIG.bank to resume)")
    check(bank, cfg)
    owner = cfg.owner or boa.env.eoa
    if not eq(bank.owner(), owner) and eq(bank.owner(), boa.env.eoa):
        bank.transfer_ownership(owner)
        print(f"ownership moved to {owner}")
    if not eq(bank.owner(), owner):
        raise RuntimeError(f"bank is owned by {bank.owner()}, expected {owner}")
    return bank


def deliver_live(bank):
    """Dry run only: deliver a live update through the deployed verifier, and check that
    the bank stores Pyth's own parse."""
    if not pyth.api_key():
        print("PYTH_API_KEY is not set: no live delivery")
        return
    response = pyth.fetch()
    # A fork's block can trail the wall clock by more than the bank's 6 s future
    # window; on mainnet the delivery lands in a block after the message.
    message_s = -(-int(response["parsed"]["timestampUs"]) // 1_000_000)
    boa.env.evm.patch.timestamp = max(boa.env.evm.patch.timestamp, message_s)
    bank.deliver_report(pyth.update_bytes(response), value=bank.verification_fee())
    for feed_id, report in pyth.reports(response).items():
        stored = bank.latest(feed_id, pyth.CHANNEL)
        if stored != report:
            raise RuntimeError(f"feed {feed_id}: stored {stored}, Pyth parsed {report}")
        print(f"feed {feed_id}: price {report[0] / 1e18:,.2f}, stored")


def verify(bank, cfg):
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        print("ETHERSCAN_API_KEY is not set: source not verified")
        return
    from boa.explorer import Etherscan

    try:
        boa.verify(bank, verifier=Etherscan(api_key=key, chain_id=cfg.chain_id), wait=True)
        print(f"verified: https://etherscan.io/address/{bank.address}#code")
    except Exception as exc:  # noqa: BLE001
        # The onchain steps are done; only verification needs a retry. Boa puts the
        # key in its status URL, which an HTTP error repeats.
        reason = str(exc).replace(key, "<ETHERSCAN_API_KEY>")
        print(f"verification failed: {reason}. Rerun with CONFIG.bank set.")


def main(args, cfg=CONFIG):
    real = mode(args)
    connect(cfg.rpc_url, cfg.chain_id, real, cfg.deployer, lambda: yubi_account(cfg.deployer))
    bank = deploy(cfg)
    if real:
        verify(bank, cfg)
    else:
        deliver_live(bank)
        print("dry run done: nothing was sent")


if __name__ == "__main__":
    main(sys.argv[1:])
