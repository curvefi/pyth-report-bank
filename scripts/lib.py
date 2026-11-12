"""Helpers shared by the scripts: the sender's key, and a real or forked network."""

import os
import sys
from urllib.parse import urlsplit

import boa


def eq(a, b):
    return str(a).lower() == str(b).lower()


def yubi_account(address):
    """The account at `address`, opened with a YubiKey touch and then the keystore password
    (scripts/yubi_deployers.py, a verbatim copy from the yubi-deployers project; the key
    file is ~/.config/eth/<address>.json.age). It never writes anything."""
    from scripts.yubi_deployers import LoadError, load_account

    try:
        return load_account(address)
    except LoadError as error:
        sys.exit(f"not loaded: {error}")


def plain_account(env_var):
    """A key kept unencrypted in the environment, for a sender that holds little."""
    from eth_account import Account

    return Account.from_key(os.environ.get(env_var) or sys.exit(f"{env_var} is not set"))


def mode(args):
    """True for a real run ("real"), False for a dry run (no argument)."""
    if args not in ([], ["real"]):
        sys.exit(f"usage: uv run python {sys.argv[0]} [real]")
    return args == ["real"]


def connect(rpc_url, chain_id, real, sender, load):
    """A real network with the account `load` returns as sender, or a fork of it with a
    stand-in sender. `sender`, if set, is the address the key must belong to, and the dry
    run's sender."""
    if not rpc_url:
        sys.exit("export ETH_RPC_URL=...")
    host = urlsplit(rpc_url).hostname  # RPC URLs often carry a key in the path
    if real:
        boa.set_network_env(rpc_url)
        # Traces are diagnostics only; a lagging RPC node must not abort after a mined tx.
        boa.env.suppress_debug_tt()
    else:
        boa.fork(rpc_url, block_identifier="latest")
    if boa.env.evm.patch.chain_id != chain_id:
        sys.exit(f"{host} is chain {boa.env.evm.patch.chain_id}, not {chain_id}")
    if real:
        acct = load()
        if sender and not eq(acct.address, sender):
            sys.exit(f"the key belongs to {acct.address}, not {sender}")
        boa.env.add_account(acct)
        boa.env.eoa = acct.address
        print(f"REAL transactions on {host} from {acct.address}")
    else:
        boa.env.eoa = sender or boa.env.generate_address("sender")
        if not sender:
            boa.env.set_balance(boa.env.eoa, 10**18)
        print(
            f"dry run on a fork of {host} at block {boa.env.evm.patch.block_number} as {boa.env.eoa}"
        )
