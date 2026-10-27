import pytest
import json
from datetime import datetime, timedelta, timezone
from instascrap import main as main_module
from instascrap.browser import BlockedError, LoggedOutError
from instascrap.config import Config
from instascrap.main import run_once, run_session
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

# Real Instagram node shape (see tests/fixtures/real_profile.json and
# instascrap.parse) -- media_type 1 is a plain image, candidates carry their own
# width/height, caption is a dict-or-null, counts are flat ints.
PAYLOAD = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
    {"node": {"code": "AAA", "taken_at": 1756729800, "media_type": 1,
              "caption": None, "like_count": 1, "comment_count": 0,
              "image_versions2": {"candidates": [
                  {"url": "https://cdn.test/AAA.jpg", "width": 100, "height": 100}]}}}]}}}

# The payload that actually arrived for michellemonaghan in production: the home
# feed's container (left over from warm_up), never the profile grid's. Same
# general node shape, wrong container key entirely -- this is what a stray
# response looks like, not a malformed one.
HOME_FEED_PAYLOAD = {"data": {"xdt_api__v1__feed__timeline__connection": {"edges": [
    {"node": {"code": "ZZZ", "taken_at": 1756729800, "media_type": 1,
              "image_versions2": {"candidates": [
                  {"url": "https://cdn.test/ZZZ.jpg", "width": 100, "height": 100}]}}}]}}}

def cfg_for(tmp_path, accounts=("alice", "bob")):
    return Config(accounts=accounts, data_dir=tmp_path / "data",
                  publish_dir=tmp_path / "pub", public_base_url="https://example.test",
                  public_prefix="0123456789abcdef", accounts_per_session=2,
                  feed_items=50, min_gap_seconds=0, max_gap_seconds=0)

def use_env(tmp_path, monkeypatch):
    env = {"ACCOUNTS": "alice,bob", "PUBLIC_URL": "https://example.test",
           "VNC_PASSWORD": "testpass", "FEED_SECRET": "0123456789abcdef",
           "ACCOUNTS_PER_SESSION": "2", "MIN_GAP_SECONDS": "0", "MAX_GAP_SECONDS": "0",
           "INSTASCRAP_DATA_DIR": str(tmp_path / "data")}
    for name, value in env.items():
        monkeypatch.setenv(name, value)

class FakeSession:
    def __init__(self, raise_on=None, warm_up_raises=False, media_fails_on=None,
                 empty_for=(), payloads=None, logout_on=None):
        self.raise_on = raise_on
        self.logout_on = logout_on
        self.warm_up_raises = warm_up_raises
        self.media_fails_on = media_fails_on
        self.empty_for = empty_for
        self.payloads = payloads
        self.visited = []
        self.warmed = False
        self.fetched = []

    def warm_up(self):
        if self.warm_up_raises:
            raise BlockedError("HTTP 429 on warm-up")
        self.warmed = True

    def visit_profile(self, account):
        if self.raise_on == account:
            raise BlockedError("HTTP 429 on profile")
        if self.logout_on == account:
            raise LoggedOutError("redirected to /accounts/login")
        self.visited.append(account)
        if account in self.empty_for:
            return []
        return self.payloads if self.payloads is not None else [PAYLOAD]

    def fetch_media(self, url):
        self.fetched.append(url)
        if self.media_fails_on == url:
            raise RuntimeError("CDN said no")
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

def test_only_grid_payloads_are_archived_once_the_grid_arrived(tmp_path):
    # Capturing every JSON response from www.instagram.com brings ~15 unrelated
    # queries per visit (chat badges, stories, suggestions). Keeping them all
    # would grow raw/ for nothing once the visit got what it came for.
    cfg = cfg_for(tmp_path, accounts=("alice",))
    noise = {"data": {"xdt_viewer": {"user": {"id": "1"}}}}
    run_session(cfg, Store(cfg.data_dir), FakeSession(payloads=[noise, PAYLOAD, noise]), NOW)
    assert len(list((cfg.data_dir / "raw" / "alice").glob("*.json.gz"))) == 1

def test_everything_is_archived_when_the_grid_is_missing(tmp_path):
    # A missing grid is the one case where the noise is the evidence: the
    # 2026-10-17 breakage was diagnosed from exactly these leftovers.
    cfg = cfg_for(tmp_path, accounts=("alice",))
    noise = {"data": {"xdt_viewer": {"user": {"id": "1"}}}}
    run_session(cfg, Store(cfg.data_dir), FakeSession(payloads=[noise, HOME_FEED_PAYLOAD]), NOW)
    assert len(list((cfg.data_dir / "raw" / "alice").glob("*.json.gz"))) == 2

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
    use_env(tmp_path, monkeypatch)
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

    exit_code = main_module.main(["--run"])

    assert exit_code == 0
    assert constructions == []  # Chrome must never be launched during a backoff


# --- one bad account must not wedge the rotation -----------------------------

def test_media_failure_does_not_stop_the_other_accounts(tmp_path):
    # Before this, a transient CDN failure propagated out of run_session before
    # mark_visited ran: the account kept its old timestamp, sorted first again on
    # the next run, failed at the same URL, and no other account was ever visited.
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    session = FakeSession(media_fails_on="https://cdn.test/AAA.jpg")
    result = run_session(cfg, store, session, NOW)
    assert session.visited == ["alice", "bob"]
    assert result.visited == ["alice", "bob"]
    assert result.new_posts == 0
    assert len(result.failures) == 2


def test_a_failing_account_is_still_marked_visited_so_rotation_advances(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(media_fails_on="https://cdn.test/AAA.jpg"), NOW)
    visited = json.loads((cfg.data_dir / "state.json").read_text())["visited"]
    assert set(visited) == {"alice", "bob"}


def test_a_failing_post_does_not_lose_the_rest_of_the_account(tmp_path):
    cfg = cfg_for(tmp_path, accounts=("alice",))
    two_posts = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": {"code": "GOOD", "taken_at": 1756729800, "media_type": 1,
                  "image_versions2": {"candidates": [
                      {"url": "https://cdn.test/GOOD.jpg", "width": 1, "height": 1}]}}},
        {"node": {"code": "BAD", "taken_at": 1756729800, "media_type": 1,
                  "image_versions2": {"candidates": [
                      {"url": "https://cdn.test/BAD.jpg", "width": 1, "height": 1}]}}},
    ]}}}
    store = Store(cfg.data_dir)
    session = FakeSession(media_fails_on="https://cdn.test/BAD.jpg", payloads=[two_posts])
    result = run_session(cfg, store, session, NOW)
    assert store.known_shortcodes("alice") == {"GOOD"}
    assert result.new_posts == 1
    assert result.failures and "BAD" in result.failures[0]


def test_block_still_short_circuits_even_though_failures_are_isolated(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    session = FakeSession(raise_on="alice")
    result = run_session(cfg, store, session, NOW)
    assert result.blocked is not None
    assert result.visited == []
    assert store.blocked_until() == NOW + timedelta(hours=24)


def test_empty_visit_is_reported_not_swallowed(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    result = run_session(cfg, store, FakeSession(empty_for=("alice",)), NOW)
    assert result.grid_missing == ["alice"]
    assert result.visited == ["alice", "bob"]   # still rotates on


def test_stray_home_feed_payload_does_not_mask_a_missing_grid(tmp_path):
    # The actual production defect: michellemonaghan's visit captured *a*
    # payload (the home feed's, left over from warm_up), just never the
    # profile grid's. `payloads` is non-empty, so the old "empty_visits" check
    # (zero payloads captured) would have reported nothing wrong at all --
    # this is the regression test for the fix that replaced it.
    cfg = cfg_for(tmp_path, accounts=("alice",))
    store = Store(cfg.data_dir)
    result = run_session(cfg, store, FakeSession(payloads=[HOME_FEED_PAYLOAD]), NOW)
    assert result.grid_missing == ["alice"]
    assert result.new_posts == 0
    assert result.dropped_nodes == 0  # a wrong container, not a malformed node


def test_a_quiet_profile_with_no_new_posts_is_not_flagged_as_grid_missing(tmp_path):
    # The false-positive guard: a profile that genuinely has nothing new still
    # sends back a grid payload (its edges are just all already known), and
    # that must stay silent -- the whole point of grid_missing is to notify
    # on the anomaly, not on the normal case it exists to be distinguished
    # from.
    cfg = cfg_for(tmp_path, accounts=("alice",))
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(payloads=[PAYLOAD]), NOW)
    result = run_session(cfg, store, FakeSession(payloads=[PAYLOAD]), NOW + timedelta(hours=8))
    assert result.new_posts == 0
    assert result.grid_missing == []


def test_another_accounts_grid_arriving_mid_visit_is_not_harvested(tmp_path):
    # Production, first three days: a grid response is slow, the visit it
    # belongs to gives up, and it lands during the *next* profile's visit. The
    # payload is a perfectly well-formed profile grid, so nothing about its
    # shape says it is the wrong one -- only its authors do. 48 of the first 75
    # archived posts were filed under the wrong account this way, and the
    # account whose grid it really was looked like it had received one.
    bobs_grid = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": {"code": "BOB1", "taken_at": 1756729800, "media_type": 1,
                  "caption": None, "like_count": 1, "comment_count": 0,
                  "user": {"username": "bob"},
                  "image_versions2": {"candidates": [
                      {"url": "https://cdn.test/BOB1.jpg", "width": 100, "height": 100}]}}}]}}}
    cfg = cfg_for(tmp_path, accounts=("alice",))
    store = Store(cfg.data_dir)
    result = run_session(cfg, store, FakeSession(payloads=[bobs_grid]), NOW)

    assert result.new_posts == 0
    assert result.dropped_nodes == 0     # a foreign grid, not a malformed node
    assert result.grid_missing == ["alice"]  # alice still never got hers
    assert not (cfg.data_dir / "posts" / "alice").exists()
    # Still archived: the raw capture is what made this diagnosable at all.
    assert list((cfg.data_dir / "raw" / "alice").glob("*.json.gz"))


def test_dropped_nodes_are_counted(tmp_path):
    cfg = cfg_for(tmp_path, accounts=("alice",))
    junk = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": {"code": "NOMEDIA", "taken_at": 1756729800}},  # no media_type at all
    ]}}}
    result = run_session(cfg, Store(cfg.data_dir), FakeSession(payloads=[junk]), NOW)
    assert result.dropped_nodes == 1
    assert result.new_posts == 0


# --- the report deploy/run.sh reads back --------------------------------------



def patch_common(monkeypatch, session_factory):
    monkeypatch.setattr(main_module, "Session", session_factory)
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])


class ContextSession:
    """Wraps a FakeSession in the context-manager protocol main() expects."""
    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self.inner

    def __exit__(self, *exc):
        return False


def read_report(tmp_path):
    return json.loads((tmp_path / "data" / "last_run.json").read_text())


def test_successful_run_writes_an_ok_report_and_exits_zero(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)
    patch_common(monkeypatch, ContextSession(FakeSession()))
    assert main_module.main(["--run"]) == 0
    report = read_report(tmp_path)
    assert report["ok"] is True and report["blocked"] is False
    assert report["visited"] == 2 and report["new_posts"] == 2


def test_blocked_run_exits_two_and_says_so_in_the_report(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)
    patch_common(monkeypatch, ContextSession(FakeSession(raise_on="alice")))
    assert main_module.main(["--run"]) == 2
    report = read_report(tmp_path)
    assert report["blocked"] is True and "429" in report["reason"]


def test_crash_exits_one_and_still_writes_a_report(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)

    def explode(store, cfg):
        raise OSError("No space left on device")

    patch_common(monkeypatch, ContextSession(FakeSession()))
    monkeypatch.setattr(main_module, "publish", explode)   # the realistic failure
    assert main_module.main(["--run"]) == 1
    report = read_report(tmp_path)
    assert report["ok"] is False
    assert "No space left" in report["error"]


def test_report_fields_survive_a_quote_laden_error(tmp_path, monkeypatch):
    # run.sh reads this file with sed, which has no notion of JSON escaping.
    use_env(tmp_path, monkeypatch)

    def explode(store, cfg):
        raise RuntimeError('bad "quoted" thing\nsecond line')

    patch_common(monkeypatch, ContextSession(FakeSession()))
    monkeypatch.setattr(main_module, "publish", explode)
    main_module.main(["--run"])
    error = read_report(tmp_path)["error"]
    assert '"' not in error and "\n" not in error and "\\" not in error


def test_report_is_written_even_while_blocked_by_an_active_backoff(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)
    store = Store(tmp_path / "data")
    store.record_block("HTTP 429", NOW)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(hours=1)

    monkeypatch.setattr(main_module, "datetime", FrozenDatetime)
    patch_common(monkeypatch, ContextSession(FakeSession()))
    assert main_module.main(["--run"]) == 0
    assert "backoff" in read_report(tmp_path)["reason"]


# --- --login wiring (run_login itself is covered in test_login.py) -----------

def test_login_mode_forces_a_login_and_exits_ok_once_signed_in(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)
    seen = []

    def fake_run_login(cfg, store, alerter):
        seen.append(store.login_required())
        store.clear_login()

    monkeypatch.setattr(main_module, "run_login", fake_run_login)
    assert main_module.main(["--login"]) == main_module.EXIT_OK
    assert seen == ["manual login"]


def test_login_mode_exits_blocked_when_sign_in_was_throttled(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)

    def fake_run_login(cfg, store, alerter):
        store.record_block("HTTP 429", datetime.now(timezone.utc))

    monkeypatch.setattr(main_module, "run_login", fake_run_login)
    assert main_module.main(["--login"]) == main_module.EXIT_BLOCKED


# --- a logged-out session is not a block --------------------------------------

def test_logout_requires_login_without_backoff(tmp_path):
    store = Store(tmp_path / "data")
    result = run_session(cfg_for(tmp_path), store, FakeSession(logout_on="alice"), NOW)
    assert result.logged_out and result.blocked is None
    assert store.blocked_until() is None
    assert store.login_required() == "redirected to /accounts/login"


def test_logout_keeps_an_expired_block_for_escalation(tmp_path):
    # The run never got far enough to prove the account healthy, so the old
    # block record stays: a block right after re-login must still escalate.
    store = Store(tmp_path / "data")
    store.record_block("HTTP 429", NOW - timedelta(hours=30))
    run_session(cfg_for(tmp_path), store, FakeSession(logout_on="alice"), NOW)
    assert store._read()["block"] is not None


# --- alerts raised by one scheduled run ----------------------------------------

class RecordingAlerter:
    def __init__(self):
        self.sent = []

    def send(self, level, title, message):
        self.sent.append((level, title))


def test_all_grids_missing_sends_capture_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])
    alerter = RecordingAlerter()
    cfg = cfg_for(tmp_path)
    run_once(cfg, Store(cfg.data_dir), alerter, NOW,
             session_factory=ContextSession(FakeSession(empty_for=("alice", "bob"))))
    assert ("alert", "Capture broken") in alerter.sent


def test_some_grids_missing_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])
    alerter = RecordingAlerter()
    cfg = cfg_for(tmp_path)
    run_once(cfg, Store(cfg.data_dir), alerter, NOW,
             session_factory=ContextSession(FakeSession(empty_for=("alice",))))
    assert alerter.sent == [("warning", "Missing grids")]


def test_block_alert_names_resume_time(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])
    alerter = RecordingAlerter()
    cfg = cfg_for(tmp_path)
    run_once(cfg, Store(cfg.data_dir), alerter, NOW,
             session_factory=ContextSession(FakeSession(raise_on="alice")))
    assert alerter.sent == [("alert", "Instagram is throttling")]


def test_quiet_successful_run_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])
    alerter = RecordingAlerter()
    cfg = cfg_for(tmp_path)
    run_once(cfg, Store(cfg.data_dir), alerter, NOW, session_factory=ContextSession(FakeSession()))
    assert alerter.sent == []


def test_crashed_run_alerts_and_reraises(tmp_path, monkeypatch):
    def explode(store, cfg):
        raise OSError("No space left on device")
    monkeypatch.setattr(main_module, "publish", explode)
    alerter = RecordingAlerter()
    cfg = cfg_for(tmp_path)
    try:
        run_once(cfg, Store(cfg.data_dir), alerter, NOW, session_factory=ContextSession(FakeSession()))
    except OSError:
        pass
    else:
        raise AssertionError("run_once must re-raise")
    assert alerter.sent == [("alert", "Run crashed")]


def test_config_error_exits_1_without_traceback(monkeypatch, capsys):
    for name in ("ACCOUNTS", "PUBLIC_URL", "VNC_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert main_module.main(["--run"]) == 1
    captured = capsys.readouterr()
    assert "ACCOUNTS" in captured.err and "Traceback" not in captured.err


def test_capture_on_a_logged_out_session_requires_login_not_backoff(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)

    class LoggedOut:
        def visit_profile(self, account):
            raise LoggedOutError("redirected to /accounts/login")

    monkeypatch.setattr(main_module, "Session", ContextSession(LoggedOut()))
    assert main_module.main(["--capture", "alice"]) == main_module.EXIT_FAILED
    store = Store(tmp_path / "data")
    assert store.blocked_until() is None and store.login_required()


# --- manual commands must respect what the daemon respects ---------------------

def tracking_session(constructions):
    class Tracking:
        def __init__(self, *args, **kwargs):
            constructions.append(args)
    return Tracking


def test_capture_refuses_during_a_backoff(tmp_path, monkeypatch):
    use_env(tmp_path, monkeypatch)
    Store(tmp_path / "data").record_block("HTTP 429", datetime.now(timezone.utc))
    constructions = []
    monkeypatch.setattr(main_module, "Session", tracking_session(constructions))
    assert main_module.main(["--capture", "alice"]) == main_module.EXIT_BLOCKED
    assert constructions == []


@pytest.mark.parametrize("mode", [["--run"], ["--capture", "alice"]])
def test_manual_modes_refuse_while_a_login_is_pending(tmp_path, monkeypatch, mode):
    use_env(tmp_path, monkeypatch)
    Store(tmp_path / "data").require_login("expired", NOW)
    constructions = []
    monkeypatch.setattr(main_module, "Session", tracking_session(constructions))
    assert main_module.main(mode) == main_module.EXIT_FAILED
    assert constructions == []


def test_manual_modes_refuse_root_when_puid_is_not_root(tmp_path, monkeypatch, capsys):
    use_env(tmp_path, monkeypatch)
    monkeypatch.setattr(main_module.os, "geteuid", lambda: 0)
    assert main_module.main(["--run"]) == main_module.EXIT_FAILED
    assert "--user 99:100" in capsys.readouterr().err


def test_manual_run_refuses_while_the_daemon_holds_the_profile(tmp_path, monkeypatch, capsys):
    from instascrap.lock import session_lock
    use_env(tmp_path, monkeypatch)
    constructions = []
    monkeypatch.setattr(main_module, "Session", tracking_session(constructions))
    with session_lock(tmp_path / "data"):
        assert main_module.main(["--run"]) == main_module.EXIT_FAILED
    assert constructions == [] and "busy" in capsys.readouterr().err
