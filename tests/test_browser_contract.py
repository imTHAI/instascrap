import time
import types

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from instascrap.browser import (
    BlockedError,
    PROFILE_GRID_POLL_S,
    PROFILE_GRID_WAIT_TIMEOUT_S,
    collect_payload,
    wait_for_condition,
)

API = "https://www.instagram.com/api/v1/users/web_profile_info/?username=alice"

def test_collect_payload_raises_on_429():
    with pytest.raises(BlockedError, match="429"):
        collect_payload(429, API, None)

def test_collect_payload_returns_api_json():
    payload = {"data": {"user": {}}}
    assert collect_payload(200, API, payload) == payload

def test_collect_payload_ignores_non_api_urls():
    assert collect_payload(200, "https://www.instagram.com/static/app.js", {"a": 1}) is None

def test_collect_payload_ignores_api_url_without_json():
    assert collect_payload(200, API, None) is None

def test_collect_payload_detects_login_redirect():
    with pytest.raises(BlockedError, match="accounts/login"):
        collect_payload(200, "https://www.instagram.com/accounts/login/", None)

def test_collect_payload_detects_feedback_required():
    with pytest.raises(BlockedError, match="feedback_required"):
        collect_payload(400, API, {"message": "feedback_required"})


# --- block persistence across phases (the design's non-negotiable rule) -------
#
# Driven through _on_response with a fake response object rather than a real
# browser: this is the single guarantee the whole project rests on, so it must be
# testable without Playwright.

class FakeResponse:
    def __init__(self, status, url, payload=None):
        self.status = status
        self.url = url
        self._payload = payload
        self.headers = {"content-type": "application/json"} if payload is not None else {}

    def json(self):
        return self._payload


def make_session(tmp_path):
    from instascrap.browser import Session
    from instascrap.config import Config
    cfg = Config(accounts=("alice",), data_dir=tmp_path, publish_dir=tmp_path,
                 public_base_url="https://example.test", public_prefix="0123456789abcdef",
                 accounts_per_session=1, feed_items=50,
                 min_gap_seconds=0, max_gap_seconds=0)
    # __init__ touches no browser; __enter__ is what launches Chrome.
    return Session(cfg, tmp_path / "profile")


def test_block_seen_during_media_phase_survives_into_next_profile(tmp_path):
    session = make_session(tmp_path)
    session._on_response(FakeResponse(429, "https://scontent.cdninstagram.com/v/x.jpg"))
    # visit_profile used to clear _block on entry, then navigate anyway.
    with pytest.raises(BlockedError, match="429"):
        session.visit_profile("bob")


def test_block_stops_the_inter_profile_gap(tmp_path):
    session = make_session(tmp_path)
    session._on_response(FakeResponse(401, "https://www.instagram.com/api/v1/x"))
    with pytest.raises(BlockedError, match="401"):
        session.gap()


def test_block_stops_further_media_fetches(tmp_path):
    session = make_session(tmp_path)
    session._on_response(FakeResponse(400, "https://www.instagram.com/api/v1/x",
                                      {"message": "feedback_required"}))
    # Raises before touching self.context, which does not exist on this session.
    with pytest.raises(BlockedError, match="feedback_required"):
        session.fetch_media("https://scontent.cdninstagram.com/v/x.jpg")


def test_warm_up_is_the_only_thing_that_clears_a_block(tmp_path):
    session = make_session(tmp_path)
    session._on_response(FakeResponse(429, "https://www.instagram.com/api/v1/x"))
    assert session._block is not None
    # Every other entry point must leave it standing.
    for call in (lambda: session.visit_profile("bob"), session.gap,
                 lambda: session.fetch_media("https://cdn.test/x.jpg")):
        with pytest.raises(BlockedError):
            call()
        assert session._block is not None


# --- Session.goto's retry / block interaction ---------------------------------
#
# The pure units (retry_transient, is_transient_navigation_error) are tested in
# isolation in tests/test_retry.py. What's tested here is the seam neither of
# those tests can see: whether Session.goto's two _raise_if_blocked() checks
# actually stop a second call to page.goto() once a block has been stashed
# mid-retry. Hand-tracing said they do; this pins it down as an assertion on
# call count so a regression here fails a test instead of only a code review.
#
# session.page is a stub (goto() driven by a script of callables) rather than
# a real Playwright Page, in keeping with this file's existing convention of
# exercising Session without launching Chrome.

class ScriptedGoto:
    """Fake `page.goto`. Each call runs the next callable in `behaviors` (raise,
    stash a block via session._on_response, or return normally); a call past
    the end of the script is a test bug, not something to silently tolerate."""
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    def __call__(self, url, **kwargs):
        self.calls += 1
        if self.calls > len(self.behaviors):
            raise AssertionError(f"page.goto called an unexpected {self.calls}th time")
        self.behaviors[self.calls - 1]()


def test_block_during_a_retriable_attempt_pre_empts_the_next_goto(tmp_path, monkeypatch):
    # A block landing on the same navigation that also raised a transient
    # error (e.g. a challenge redirect that also stalls domcontentloaded) must
    # stop the retry before it fires a second request — not just eventually
    # surface the block after one more round trip to Instagram.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = make_session(tmp_path)

    def flaky_and_blocked():
        session._on_response(FakeResponse(429, "https://www.instagram.com/api/v1/x"))
        raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    goto = ScriptedGoto([flaky_and_blocked])
    session.page = types.SimpleNamespace(goto=goto)

    with pytest.raises(BlockedError, match="429"):
        session.goto("https://www.instagram.com/", wait_until="domcontentloaded")

    # ScriptedGoto would raise AssertionError on a 2nd call anyway; asserting
    # the count explicitly is what makes the intent legible in the failure.
    assert goto.calls == 1


def test_block_during_the_terminal_attempt_still_wins_over_the_transient_error(tmp_path, monkeypatch):
    # The race the second _raise_if_blocked() in Session.goto exists for:
    # retry_transient has no further attempt left to catch the block at its
    # own top, so goto() must check again before letting the transient error
    # (rather than the block) escape — and must not spend a 4th request doing it.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = make_session(tmp_path)

    def just_timeout():
        raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    def timeout_and_blocked():
        session._on_response(FakeResponse(401, "https://www.instagram.com/api/v1/x"))
        raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    # 3 attempts total (NAV_RETRY_ATTEMPTS): two ordinary transient failures,
    # then the block arrives on the last one.
    goto = ScriptedGoto([just_timeout, just_timeout, timeout_and_blocked])
    session.page = types.SimpleNamespace(goto=goto)

    with pytest.raises(BlockedError, match="401"):
        session.goto("https://www.instagram.com/", wait_until="domcontentloaded")

    assert goto.calls == 3


def test_goto_retries_a_transient_failure_and_succeeds_without_a_block(tmp_path, monkeypatch):
    # Control case: end-to-end through Session.goto (not just retry_transient
    # in isolation), a transient failure with no block in sight is retried and
    # recovers, so the two tests above aren't passing merely because goto()
    # never actually retries.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = make_session(tmp_path)

    def flaky_once():
        raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    def succeeds():
        return None

    goto = ScriptedGoto([flaky_once, succeeds])
    session.page = types.SimpleNamespace(goto=goto)

    session.goto("https://www.instagram.com/", wait_until="domcontentloaded")

    assert goto.calls == 2
    assert session._block is None


# --- wait_for_condition, the pure poll loop visit_profile is built on ---------

def test_wait_for_condition_returns_true_immediately_when_already_met():
    calls = []
    result = wait_for_condition(
        is_met=lambda: True, is_aborted=lambda: False,
        timeout_s=10.0, poll_s=1.0,
    )
    assert result is True


def test_wait_for_condition_checks_abort_before_met_on_the_very_first_poll():
    # An abort that is already true when the wait starts must win before
    # is_met is even consulted -- this is what lets Session use it to check a
    # block stashed before visit_profile's wait even begins.
    met_calls = []
    result = wait_for_condition(
        is_met=lambda: met_calls.append(1) or True,
        is_aborted=lambda: True,
        timeout_s=10.0, poll_s=1.0,
    )
    assert result is False
    assert met_calls == []


def test_wait_for_condition_times_out_without_sleeping_past_the_bound(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleeps.append(secs))
    result = wait_for_condition(
        is_met=lambda: False, is_aborted=lambda: False,
        timeout_s=1.0, poll_s=0.25,
    )
    assert result is False
    assert len(sleeps) == 4  # 1.0 / 0.25, exactly the bound, no extra poll


def test_wait_for_condition_stops_polling_the_instant_met_turns_true(monkeypatch):
    calls = []

    def is_met():
        calls.append(1)
        return len(calls) == 3

    monkeypatch.setattr(time, "sleep", lambda secs: None)
    result = wait_for_condition(
        is_met=is_met, is_aborted=lambda: False,
        timeout_s=100.0, poll_s=1.0,
    )
    assert result is True
    assert len(calls) == 3  # did not keep polling once satisfied


# --- Session.visit_profile: waits for the grid, not a flat delay -------------
#
# This is the regression coverage for the production defect: michellemonaghan's
# visit came back with only a stray home-feed payload because the flat 1.5-3.5s
# sleep this replaced returned before the grid response arrived. These drive
# visit_profile through a stubbed page.goto exactly like the goto-retry tests
# above, and inject responses via session._on_response from inside a mocked
# time.sleep to simulate a response arriving mid-wait.

GRID_PAYLOAD = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": []}}}
HOME_FEED_PAYLOAD = {"data": {"xdt_api__v1__feed__timeline__connection": {"edges": []}}}


def test_visit_profile_returns_as_soon_as_the_grid_arrives(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    session.page = types.SimpleNamespace(goto=lambda *a, **kw: None)

    sleeps = []

    def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 1:
            session._on_response(FakeResponse(200, API, GRID_PAYLOAD))

    monkeypatch.setattr(time, "sleep", fake_sleep)

    result = session.visit_profile("alice")

    assert result == [GRID_PAYLOAD]
    assert len(sleeps) == 1  # returned on the very next poll, not the full timeout


def test_visit_profile_keeps_a_stray_payload_but_flags_the_grid_as_missing_by_giving_up(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    # visit_profile clears _captured on entry, so the stray payload has to
    # arrive *after* that -- during the wait, via the goto stub, exactly like
    # warm_up's leftover home-feed response would in a real session.
    session.page = types.SimpleNamespace(
        goto=lambda *a, **kw: session._on_response(FakeResponse(200, API, HOME_FEED_PAYLOAD))
    )

    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda secs: sleeps.append(secs))

    result = session.visit_profile("alice")

    # The stray payload is not lost -- main.py's grid_missing check is what
    # turns "captured something, but not the grid" into a reported anomaly;
    # visit_profile's job is only to wait long enough and hand back whatever
    # arrived.
    assert result == [HOME_FEED_PAYLOAD]
    assert len(sleeps) == round(PROFILE_GRID_WAIT_TIMEOUT_S / PROFILE_GRID_POLL_S)


def test_visit_profile_surfaces_a_block_that_arrives_during_the_wait(tmp_path, monkeypatch):
    goto_calls = []
    session = make_session(tmp_path)
    session.page = types.SimpleNamespace(goto=lambda *a, **kw: goto_calls.append(1))

    sleeps = []

    def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 1:
            session._on_response(FakeResponse(429, "https://www.instagram.com/api/v1/x"))

    monkeypatch.setattr(time, "sleep", fake_sleep)

    with pytest.raises(BlockedError, match="429"):
        session.visit_profile("alice")

    assert goto_calls == [1]  # a single navigation -- visit_profile never retries
    assert len(sleeps) == 1   # the wait aborted immediately once the block landed
