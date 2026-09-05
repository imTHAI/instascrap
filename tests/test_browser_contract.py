import pytest
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
