import contextlib
import json
from datetime import datetime, timedelta, timezone
from instascrap import main as main_module
from instascrap.browser import BlockedError
from instascrap.config import Config
from instascrap.main import run_session
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

class FakeSession:
    def __init__(self, raise_on=None, warm_up_raises=False, media_fails_on=None,
                 empty_for=(), payloads=None):
        self.raise_on = raise_on
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


def test_dropped_nodes_are_counted(tmp_path):
    cfg = cfg_for(tmp_path, accounts=("alice",))
    junk = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": {"code": "NOMEDIA", "taken_at": 1756729800}},  # no media_type at all
    ]}}}
    result = run_session(cfg, Store(cfg.data_dir), FakeSession(payloads=[junk]), NOW)
    assert result.dropped_nodes == 1
    assert result.new_posts == 0


# --- the report deploy/run.sh reads back --------------------------------------

def write_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"""
accounts = ["alice", "bob"]
data_dir = "{tmp_path / "data"}"
publish_dir = "{tmp_path / "pub"}"
public_base_url = "https://example.test"
public_prefix = "0123456789abcdef"
accounts_per_session = 2
feed_items = 50
min_gap_seconds = 0
max_gap_seconds = 0
""")
    return config_path


def patch_common(monkeypatch, session_factory):
    monkeypatch.setattr(main_module, "Session", session_factory)
    monkeypatch.setattr(main_module, "publish", lambda store, cfg: [])
    monkeypatch.setattr(main_module, "notify", lambda *a, **kw: True)


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
    config_path = write_config(tmp_path)
    patch_common(monkeypatch, ContextSession(FakeSession()))
    assert main_module.main(["--run", "--config", str(config_path)]) == 0
    report = read_report(tmp_path)
    assert report["ok"] is True and report["blocked"] is False
    assert report["visited"] == 2 and report["new_posts"] == 2


def test_blocked_run_exits_two_and_says_so_in_the_report(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    patch_common(monkeypatch, ContextSession(FakeSession(raise_on="alice")))
    assert main_module.main(["--run", "--config", str(config_path)]) == 2
    report = read_report(tmp_path)
    assert report["blocked"] is True and "429" in report["reason"]


def test_crash_exits_one_and_still_writes_a_report(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)

    def explode(store, cfg):
        raise OSError("No space left on device")

    patch_common(monkeypatch, ContextSession(FakeSession()))
    monkeypatch.setattr(main_module, "publish", explode)   # the realistic failure
    assert main_module.main(["--run", "--config", str(config_path)]) == 1
    report = read_report(tmp_path)
    assert report["ok"] is False
    assert "No space left" in report["error"]


def test_report_fields_survive_a_quote_laden_error(tmp_path, monkeypatch):
    # run.sh reads this file with sed, which has no notion of JSON escaping.
    config_path = write_config(tmp_path)

    def explode(store, cfg):
        raise RuntimeError('bad "quoted" thing\nsecond line')

    patch_common(monkeypatch, ContextSession(FakeSession()))
    monkeypatch.setattr(main_module, "publish", explode)
    main_module.main(["--run", "--config", str(config_path)])
    error = read_report(tmp_path)["error"]
    assert '"' not in error and "\n" not in error and "\\" not in error


def test_report_is_written_even_while_blocked_by_an_active_backoff(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    store = Store(tmp_path / "data")
    store.record_block("HTTP 429", NOW)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(hours=1)

    monkeypatch.setattr(main_module, "datetime", FrozenDatetime)
    patch_common(monkeypatch, ContextSession(FakeSession()))
    assert main_module.main(["--run", "--config", str(config_path)]) == 0
    assert "backoff" in read_report(tmp_path)["reason"]


# --- --login: a block after the declared navigation must not be swallowed ----
#
# No test at all covered this branch before, in either direction, which is
# part of why the gap went unnoticed: once --login's declared navigation to
# the login page ends, nothing checked session._block again, so a real block
# landing while the operator authenticates (a challenge_required on a
# background XHR, say) was recorded nowhere and --login exited 0 as if the
# login had gone cleanly. The next scheduled --run would then walk straight
# back in with zero backoff -- the exact "insist after a refusal" pattern the
# whole project exists to prevent. This path was dead code before the
# login-suppression fix (--login used to abort on its own declared
# navigation and never reached the operator prompt at all), which is why it
# is only reachable, and only worth testing, now.

class FakeLoginSession:
    """Stands in for browser.Session in --login tests. Supports exactly the
    surface main.py's --login branch touches: expect_login_redirect, goto,
    and the public raise_if_blocked the fix depends on to notice a block
    stashed after the declared navigation ends. Session's own suppression
    logic (is_bare_login_redirect and friends) is exercised in
    test_browser_contract.py, not here -- this file's job is main.py's
    wiring around whatever Session decides."""
    def __init__(self, block_on_goto=None, block_after_input=None):
        self.block_on_goto = block_on_goto
        self.block_after_input = block_after_input
        self.goto_calls = []

    @contextlib.contextmanager
    def expect_login_redirect(self):
        yield

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if self.block_on_goto:
            raise BlockedError(self.block_on_goto)

    def raise_if_blocked(self):
        if self.block_after_input:
            raise BlockedError(self.block_after_input)


def _forbidden_input():
    raise AssertionError("must not reach the operator prompt")


def test_login_records_and_exits_blocked_when_the_declared_navigation_is_refused(tmp_path, monkeypatch):
    # A block that IS raised, rather than merely stashed -- e.g. a 401 riding
    # on the login page itself. Must never reach the operator prompt.
    config_path = write_config(tmp_path)
    patch_common(monkeypatch, ContextSession(
        FakeLoginSession(block_on_goto="HTTP 401 on accounts/login")))
    monkeypatch.setattr("builtins.input", lambda: _forbidden_input())

    assert main_module.main(["--login", "--config", str(config_path)]) == main_module.EXIT_BLOCKED
    assert Store(tmp_path / "data").blocked_until() is not None


def test_login_records_and_exits_blocked_when_a_block_lands_after_the_navigation(tmp_path, monkeypatch):
    # The reviewer's exact scenario: the declared navigation finishes clean --
    # goto() never raises -- and a real block only lands afterwards, stashed
    # in session._block while the operator is typing. Before the fix nothing
    # ever looked at _block again past this point.
    config_path = write_config(tmp_path)
    patch_common(monkeypatch, ContextSession(FakeLoginSession(
        block_after_input="challenge_required in response from https://www.instagram.com/api/v1/x")))
    monkeypatch.setattr("builtins.input", lambda: "")

    assert main_module.main(["--login", "--config", str(config_path)]) == main_module.EXIT_BLOCKED
    assert Store(tmp_path / "data").blocked_until() is not None


def test_login_exits_clean_when_no_block_ever_lands(tmp_path, monkeypatch):
    # The pin against a false alarm: an ordinary, successful login must still
    # exit 0 with no block recorded -- the fix must not turn every clean
    # --login into a false positive.
    config_path = write_config(tmp_path)
    patch_common(monkeypatch, ContextSession(FakeLoginSession()))
    monkeypatch.setattr("builtins.input", lambda: "")

    assert main_module.main(["--login", "--config", str(config_path)]) == main_module.EXIT_OK
    assert Store(tmp_path / "data").blocked_until() is None
