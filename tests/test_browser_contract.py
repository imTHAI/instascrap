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
