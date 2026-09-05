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
min_gap_seconds = {min_gap_seconds}
max_gap_seconds = {max_gap_seconds}
"""

def write_cfg(tmp_path, accounts='["alice", "bob"]', prefix="0123456789abcdef", min_gap_seconds=25, max_gap_seconds=90):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(accounts=accounts, prefix=prefix, min_gap_seconds=min_gap_seconds, max_gap_seconds=max_gap_seconds))
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
    path.write_text(BASE.format(accounts='["a"]', prefix="0123456789abcdef", min_gap_seconds=25, max_gap_seconds=90)
                    .replace("https://example.test", "https://example.test/"))
    assert load_config(path).public_base_url == "https://example.test"

def test_load_config_rejects_invalid_gap_order(tmp_path):
    with pytest.raises(ValueError, match="min_gap_seconds"):
        load_config(write_cfg(tmp_path, min_gap_seconds=100, max_gap_seconds=50))

def test_load_config_rejects_path_traversal_in_accounts(tmp_path):
    with pytest.raises(ValueError, match="account"):
        load_config(write_cfg(tmp_path, accounts='["alice", "../../etc"]'))

def test_load_config_rejects_dot_dot_account(tmp_path):
    with pytest.raises(ValueError, match="account"):
        load_config(write_cfg(tmp_path, accounts='[".."]'))

def test_load_config_rejects_slash_in_account(tmp_path):
    with pytest.raises(ValueError, match="account"):
        load_config(write_cfg(tmp_path, accounts='["a/b"]'))

def test_load_config_accepts_dots_and_underscores(tmp_path):
    assert load_config(write_cfg(tmp_path, accounts='["a.b_c", "x9"]')).accounts == ("a.b_c", "x9")

def test_load_config_reads_utf8_toml_regardless_of_locale(tmp_path):
    # TOML is UTF-8 by definition; decoding it with the platform locale is how a
    # container under LANG=C dies on an accented comment.
    path = tmp_path / "config.toml"
    body = "# préfixe généré à l'initialisation — ne pas changer\n" + BASE.format(
        accounts='["alice"]', prefix="0123456789abcdef", min_gap_seconds=25, max_gap_seconds=90)
    path.write_bytes(body.encode("utf-8"))
    assert load_config(path).accounts == ("alice",)
