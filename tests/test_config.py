import re
from datetime import time

import pytest

from instascrap.config import (ConfigError, load_config, parse_accounts, parse_schedule,
                               resolve_feed_secret)

BASE = {"ACCOUNTS": "alice,bob", "PUBLIC_URL": "https://feeds.example.com/",
        "VNC_PASSWORD": "secret12"}


def test_minimal_env_gives_spec_defaults(tmp_path):
    cfg = load_config(BASE, data_dir=tmp_path)
    assert cfg.accounts == ("alice", "bob")
    assert cfg.public_base_url == "https://feeds.example.com"
    assert cfg.data_dir == tmp_path
    assert cfg.publish_dir == tmp_path / "public"
    assert (cfg.accounts_per_session, cfg.feed_items) == (7, 50)
    assert (cfg.min_gap_seconds, cfg.max_gap_seconds, cfg.jitter_minutes) == (25, 90, 30)
    assert cfg.schedule == (time(8), time(14), time(21))
    assert cfg.discord_webhook is None and cfg.tz == "UTC"
    assert re.fullmatch(r"[0-9a-f]{16}", cfg.public_prefix)


def test_accounts_are_cleaned():
    assert parse_accounts(" @nasagoddard, map_of_europe,,nasagoddard ") == ("nasagoddard", "map_of_europe")


@pytest.mark.parametrize("raw", ["", " , ", "bad name", "..", "alice,../../etc"])
def test_bad_accounts_are_rejected(raw):
    with pytest.raises(ConfigError, match="ACCOUNTS"):
        parse_accounts(raw)


def test_schedule_accepts_single_digit_hour():
    assert parse_schedule("21:00, 8:00") == (time(8), time(21))


def test_schedule_drops_duplicates():
    assert parse_schedule("08:00,8:00") == (time(8),)


@pytest.mark.parametrize("raw", ["8h", "25:00", "08:00;14:00", "", "08:60"])
def test_bad_schedule_is_rejected(raw):
    with pytest.raises(ConfigError, match="SCHEDULE"):
        parse_schedule(raw)


@pytest.mark.parametrize("missing", ["ACCOUNTS", "PUBLIC_URL", "VNC_PASSWORD"])
def test_required_variables(tmp_path, missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(env, data_dir=tmp_path)


@pytest.mark.parametrize("url", ["feeds.example.com", "ftp://x", "https://"])
def test_public_url_needs_http_scheme_and_host(tmp_path, url):
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        load_config({**BASE, "PUBLIC_URL": url}, data_dir=tmp_path)


def test_long_vnc_password_warns(tmp_path, caplog):
    load_config({**BASE, "VNC_PASSWORD": "123456789"}, data_dir=tmp_path)
    assert "only the first 8 characters" in caplog.text


def test_feed_secret_is_generated_once_and_persisted(tmp_path):
    first = resolve_feed_secret("", tmp_path)
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert resolve_feed_secret("", tmp_path) == first
    assert (tmp_path / "feed_secret").read_text().strip() == first


def test_explicit_feed_secret_wins_and_is_validated(tmp_path):
    (tmp_path / "feed_secret").write_text("aaaaaaaaaaaaaaaa")
    assert resolve_feed_secret("3f9a1c0b7d2e4a68", tmp_path) == "3f9a1c0b7d2e4a68"
    with pytest.raises(ConfigError, match="FEED_SECRET"):
        resolve_feed_secret("NOTHEX", tmp_path)


def test_gap_bounds_are_checked(tmp_path):
    with pytest.raises(ConfigError, match="MIN_GAP_SECONDS"):
        load_config({**BASE, "MIN_GAP_SECONDS": "100"}, data_dir=tmp_path)


def test_non_integer_is_named(tmp_path):
    with pytest.raises(ConfigError, match="FEED_ITEMS"):
        load_config({**BASE, "FEED_ITEMS": "lots"}, data_dir=tmp_path)


def test_empty_discord_webhook_means_none_and_host_is_read(tmp_path):
    cfg = load_config({**BASE, "DISCORD_WEBHOOK": " ", "HOST_HOSTNAME": "Tower",
                       "TZ": "Europe/Paris"}, data_dir=tmp_path)
    assert cfg.discord_webhook is None
    assert (cfg.host_hostname, cfg.tz) == ("Tower", "Europe/Paris")


def test_unknown_timezone_is_named(tmp_path):
    with pytest.raises(ConfigError, match="TZ"):
        load_config({**BASE, "TZ": "Mars/Olympus"}, data_dir=tmp_path)


@pytest.mark.parametrize("password", ["#secret1", "__EMPTY__", "__BEGIN_VIEWONLY__", "a\nb"])
def test_vnc_password_that_x11vnc_would_misread_is_rejected(tmp_path, password):
    # x11vnc's -passwdfile treats "#" lines as comments and "__...__" lines as
    # directives: such a password could leave the login screen without one.
    with pytest.raises(ConfigError, match="VNC_PASSWORD"):
        load_config({**BASE, "VNC_PASSWORD": password}, data_dir=tmp_path)
