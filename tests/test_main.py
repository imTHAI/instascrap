import json
from datetime import datetime, timedelta, timezone
from instascrap import main as main_module
from instascrap.browser import BlockedError
from instascrap.config import Config
from instascrap.main import run_session
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

PAYLOAD = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": [
    {"node": {"shortcode": "AAA", "taken_at_timestamp": 1756729800, "is_video": False,
              "display_url": "https://cdn.test/AAA.jpg",
              "dimensions": {"width": 100, "height": 100},
              "edge_media_to_caption": {"edges": []},
              "edge_liked_by": {"count": 1},
              "edge_media_to_comment": {"count": 0}}}]}}}}

def cfg_for(tmp_path, accounts=("alice", "bob")):
    return Config(accounts=accounts, data_dir=tmp_path / "data",
                  publish_dir=tmp_path / "pub", public_base_url="https://example.test",
                  public_prefix="0123456789abcdef", accounts_per_session=2,
                  feed_items=50, min_gap_seconds=0, max_gap_seconds=0)

class FakeSession:
    def __init__(self, raise_on=None, warm_up_raises=False):
        self.raise_on = raise_on
        self.warm_up_raises = warm_up_raises
        self.visited = []
        self.warmed = False

    def warm_up(self):
        if self.warm_up_raises:
            raise BlockedError("HTTP 429 on warm-up")
        self.warmed = True

    def visit_profile(self, account):
        if self.raise_on == account:
            raise BlockedError("HTTP 429 on profile")
        self.visited.append(account)
        return [PAYLOAD]

    def fetch_media(self, url):
        return b"bytes"

    def gap(self):
        pass

def test_session_warms_up_before_visiting(tmp_path):
    cfg = cfg_for(tmp_path)
    session = FakeSession()
    run_session(cfg, Store(cfg.data_dir), session, NOW)
    assert session.warmed is True

def test_new_posts_are_archived(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    result = run_session(cfg, store, FakeSession(), NOW)
    assert result.new_posts == 2  # the same payload is harvested for both accounts
    assert store.known_shortcodes("alice") == {"AAA"}

def test_already_known_posts_are_not_refetched(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(), NOW)
    result = run_session(cfg, store, FakeSession(), NOW + timedelta(hours=8))
    assert result.new_posts == 0

def test_block_stops_the_session_immediately(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    session = FakeSession(raise_on="alice")
    result = run_session(cfg, store, session, NOW)
    assert result.blocked is not None
    assert session.visited == []                       # no fallthrough to bob
    assert store.blocked_until() == NOW + timedelta(hours=24)

def test_session_refuses_to_run_while_blocked(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.record_block("HTTP 429", NOW)
    session = FakeSession()
    result = run_session(cfg, store, session, NOW + timedelta(hours=1))
    assert result.visited == []
    assert session.warmed is False

def test_expired_block_is_cleared_and_session_runs(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.record_block("HTTP 429", NOW)
    result = run_session(cfg, store, FakeSession(), NOW + timedelta(hours=25))
    assert result.visited != []
    assert store.blocked_until() is None

def test_successful_session_marks_accounts_visited(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(), NOW)
    visited = json.loads((cfg.data_dir / "state.json").read_text())["visited"]
    assert set(visited) == {"alice", "bob"}

def test_raw_payloads_are_archived(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(), NOW)
    assert list((cfg.data_dir / "raw" / "alice").glob("*.json.gz"))

def test_block_during_warmup_stops_before_any_profile_visit(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    session = FakeSession(warm_up_raises=True)
    result = run_session(cfg, store, session, NOW)
    assert result.blocked is not None
    assert session.visited == []
    assert store.blocked_until() == NOW + timedelta(hours=24)

def test_block_right_after_expiry_escalates_to_72h(tmp_path):
    # A block that has just expired is a *consecutive* block, not a fresh one:
    # getting refused again immediately must climb the backoff ladder to 72h,
    # not reset it to 24h. That only holds if the expired record is left in
    # place for record_block to see, rather than cleared before the retry.
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.record_block("HTTP 429", NOW)
    retry_at = NOW + timedelta(hours=25)
    session = FakeSession(raise_on="alice")
    result = run_session(cfg, store, session, retry_at)
    assert result.blocked is not None
    assert store.blocked_until() == retry_at + timedelta(hours=72)

def test_main_run_does_not_open_browser_while_blocked(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"""
accounts = ["alice", "bob"]
data_dir = "{data_dir}"
publish_dir = "{tmp_path / "pub"}"
public_base_url = "https://example.test"
public_prefix = "0123456789abcdef"
accounts_per_session = 2
feed_items = 50
min_gap_seconds = 0
max_gap_seconds = 0
""")
    store = Store(data_dir)
    store.record_block("HTTP 429", NOW)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(hours=1)  # still well within the 24h backoff

    constructions = []

    class TrackingSession:
        def __init__(self, *args, **kwargs):
            constructions.append((args, kwargs))

    monkeypatch.setattr(main_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(main_module, "Session", TrackingSession)
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])
    monkeypatch.setattr(main_module, "notify", lambda *a, **kw: True)

    exit_code = main_module.main(["--run", "--config", str(config_path)])

    assert exit_code == 0
    assert constructions == []  # Chrome must never be launched during a backoff
