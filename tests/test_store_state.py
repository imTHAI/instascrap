from datetime import datetime, timedelta, timezone
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

def test_never_visited_accounts_come_first(tmp_path):
    store = Store(tmp_path)
    store.mark_visited("alice", NOW)
    order = store.visit_order(["alice", "bob", "carol"], 3)
    assert order[-1] == "alice"
    assert set(order[:2]) == {"bob", "carol"}

def test_visit_order_respects_limit(tmp_path):
    assert len(Store(tmp_path).visit_order(["a", "b", "c", "d"], 2)) == 2

def test_oldest_visit_wins(tmp_path):
    store = Store(tmp_path)
    store.mark_visited("alice", NOW - timedelta(days=2))
    store.mark_visited("bob", NOW - timedelta(hours=1))
    assert store.visit_order(["alice", "bob"], 1) == ["alice"]

def test_state_survives_reinstantiation(tmp_path):
    Store(tmp_path).mark_visited("alice", NOW)
    assert Store(tmp_path).visit_order(["alice", "bob"], 1) == ["bob"]

def test_no_block_by_default(tmp_path):
    assert Store(tmp_path).blocked_until() is None

def test_first_block_backs_off_24h(tmp_path):
    assert Store(tmp_path).record_block("HTTP 429", NOW) == NOW + timedelta(hours=24)

def test_second_consecutive_block_backs_off_72h(tmp_path):
    store = Store(tmp_path)
    store.record_block("HTTP 429", NOW)
    assert store.record_block("HTTP 429", NOW) == NOW + timedelta(hours=72)

def test_clear_block_resets_the_streak(tmp_path):
    store = Store(tmp_path)
    store.record_block("HTTP 429", NOW)
    store.clear_block()
    assert store.blocked_until() is None
    assert store.record_block("HTTP 429", NOW) == NOW + timedelta(hours=24)

def test_last_new_post_tracks_most_recent(tmp_path):
    store = Store(tmp_path)
    assert store.last_new_post_at() is None
    store.mark_new_post(NOW)
    assert store.last_new_post_at() == NOW


def test_watchdog_clock_is_seeded_at_first_creation(tmp_path):
    # Before this, a fresh install with a broken parser never tripped the 7-day
    # watchdog, because last_new_post is only ever set once a post lands.
    store = Store(tmp_path)
    assert store.last_new_post_at() is None
    seeded = store.silence_since()
    assert seeded is not None
    assert (datetime.now(timezone.utc) - seeded) < timedelta(minutes=5)


def test_silence_since_follows_the_last_post_once_one_lands(tmp_path):
    store = Store(tmp_path)
    store.mark_new_post(NOW)
    assert store.silence_since() == NOW


def test_seeded_clock_survives_reinstantiation(tmp_path):
    first = Store(tmp_path).silence_since()
    assert Store(tmp_path).silence_since() == first


def test_corrupt_state_does_not_crash_and_is_set_aside(tmp_path):
    store = Store(tmp_path)
    store.mark_visited("alice", NOW)
    store.state_path.write_text("{ this is not json", encoding="utf-8")
    assert store.visit_order(["alice", "bob"], 2) == ["alice", "bob"]
    assert store.blocked_until() is None
    assert (tmp_path / "state.json.corrupt").exists()


def test_schema_drift_keeps_the_fields_that_are_still_valid(tmp_path):
    store = Store(tmp_path)
    store.state_path.write_text(
        '{"visited": {"alice": "2026-09-04T09:00:00+00:00"}, "block": "surprise"}',
        encoding="utf-8")
    assert store.blocked_until() is None          # bad shape, dropped
    assert store.visit_order(["alice", "bob"], 1) == ["bob"]   # good shape, kept


def test_unparseable_block_deadline_is_ignored_not_raised(tmp_path):
    store = Store(tmp_path)
    store.state_path.write_text('{"block": {"until": "pas une date"}}', encoding="utf-8")
    assert store.blocked_until() is None


def test_state_is_read_as_utf8_regardless_of_locale(tmp_path):
    # Block reasons embed URLs, so state.json can legitimately hold non-ASCII.
    # Reading it with the platform locale would blow up under LANG=C.
    store = Store(tmp_path)
    store.state_path.write_bytes(
        '{"visited": {"alicé—ü": "2026-09-04T09:00:00+00:00"}}'.encode("utf-8"))
    assert store.visit_order(["alicé—ü", "bob"], 1) == ["bob"]


def test_login_state_round_trip(tmp_path):
    store = Store(tmp_path)
    assert store.login_required() is None
    store.require_login("redirected to /accounts/login", NOW)
    assert store.login_required() == "redirected to /accounts/login"
    store.mark_login_alert(NOW)
    assert store.last_login_alert() == NOW
    store.clear_login()
    assert store.login_required() is None and store.last_login_alert() is None


def test_state_without_login_key_reads_as_logged_in(tmp_path):
    # Installs predating this key (tower) must not be asked to log in again.
    (tmp_path / "state.json").write_text('{"visited": {}, "block": null}')
    assert Store(tmp_path).login_required() is None


def test_malformed_login_entry_is_reset(tmp_path):
    (tmp_path / "state.json").write_text('{"login": "oops"}')
    assert Store(tmp_path).login_required() is None
