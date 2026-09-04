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
