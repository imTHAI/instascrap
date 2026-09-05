import time
import types

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from instascrap.browser import BlockedError, collect_payload

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
