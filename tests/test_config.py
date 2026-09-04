from pathlib import Path
import pytest
from instascrap.config import load_config

BASE = """
accounts = {accounts}
data_dir = "/data"
publish_dir = "/publish"
public_base_url = "https://example.test"
public_prefix = "{prefix}"
accounts_per_session = 7
feed_items = 50
min_gap_seconds = 25
max_gap_seconds = 90
"""

def write_cfg(tmp_path, accounts='["alice", "bob"]', prefix="0123456789abcdef"):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(accounts=accounts, prefix=prefix))
    return path

def test_load_config_reads_all_fields(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.accounts == ("alice", "bob")
    assert cfg.data_dir == Path("/data")
    assert cfg.public_prefix == "0123456789abcdef"
    assert cfg.accounts_per_session == 7

def test_load_config_rejects_bad_prefix(tmp_path):
    with pytest.raises(ValueError, match="public_prefix"):
        load_config(write_cfg(tmp_path, prefix="trop-court"))

def test_load_config_rejects_empty_accounts(tmp_path):
    with pytest.raises(ValueError, match="accounts"):
        load_config(write_cfg(tmp_path, accounts="[]"))

def test_load_config_strips_trailing_slash_from_base_url(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(accounts='["a"]', prefix="0123456789abcdef")
                    .replace("https://example.test", "https://example.test/"))
    assert load_config(path).public_base_url == "https://example.test"
