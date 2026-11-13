"""scripts/deploy.py against the port of Pyth's verifier: deploy, resume, checks."""

from types import SimpleNamespace

import boa
import pytest
from conftest import FEE, FOREVER, PYTH, TEST_KEY

from scripts import deploy as script


def config(**changes):
    """The script's CONFIG on a port verifier, deploying a new bank; the deployer keeps
    ownership unless `owner` is passed."""
    verifier = PYTH.deploy(TEST_KEY.public_key.to_checksum_address(), FOREVER, FEE).address
    return SimpleNamespace(
        **{**vars(script.CONFIG), "verifier": verifier, "owner": None, "bank": None, **changes}
    )


def test_deploy_keeps_or_hands_over_ownership():
    cfg = config()
    bank = script.deploy(cfg)
    assert bank.owner() == boa.env.eoa
    assert bank.verifier() == cfg.verifier
    dao = boa.env.generate_address()
    assert script.deploy(config(owner=dao)).owner() == dao


def test_resume_checks_the_deployment():
    cfg = config()
    bank = script.deploy(cfg)
    dao = boa.env.generate_address()
    # Resuming finishes the handover and rebuilds the constructor arguments.
    resumed = script.deploy(SimpleNamespace(**{**vars(cfg), "bank": bank.address, "owner": dao}))
    assert resumed.address == bank.address
    assert resumed.owner() == dao
    assert resumed.ctor_calldata == bank.ctor_calldata
    # Owned by neither the sender nor CONFIG.owner, with or without a handover: refused.
    for owner in (None, cfg.verifier):
        with pytest.raises(RuntimeError, match="owned by"):
            script.deploy(SimpleNamespace(**{**vars(cfg), "bank": bank.address, "owner": owner}))
    with pytest.raises(RuntimeError, match="verifier is"):
        script.deploy(config(bank=bank.address))
    with pytest.raises(RuntimeError, match="bytecode"):
        script.deploy(SimpleNamespace(**{**vars(cfg), "bank": cfg.verifier}))


def test_verification_failure_hides_the_etherscan_key(monkeypatch, capsys):
    key = "SECRETKEY123"
    monkeypatch.setenv("ETHERSCAN_API_KEY", key)

    def fail(*args, **kwargs):
        raise RuntimeError(f"503 for url: https://api.etherscan.io/v2/api?apikey={key}&chainid=1")

    monkeypatch.setattr(boa, "verify", fail)
    script.verify(script.deploy(config()), script.CONFIG)
    out = capsys.readouterr().out
    assert "verification failed" in out
    assert key not in out


def test_only_real_or_nothing_is_accepted():
    for args in (["--real"], ["real", "now"]):
        with pytest.raises(SystemExit, match="usage"):
            script.main(args)


def test_yubikey_loader_refuses_an_address_without_a_key_file(monkeypatch, tmp_path):
    # The key-file check comes before any YubiKey or password prompt.
    from scripts import yubi_deployers

    monkeypatch.setattr(yubi_deployers, "KEY_DIR", tmp_path)
    with pytest.raises(SystemExit, match="not loaded: no key file"):
        script.yubi_account(boa.env.generate_address())
