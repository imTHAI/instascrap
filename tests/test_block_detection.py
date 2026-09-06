from instascrap.parse import detect_block, is_bare_login_redirect

OK_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username=alice"
LOGIN_URL = "https://www.instagram.com/accounts/login/?next=/alice/"

def test_normal_response_is_not_a_block():
    assert detect_block(200, {"status": "ok"}, OK_URL) is None

def test_401_is_a_block():
    assert "401" in detect_block(401, None, OK_URL)

def test_429_is_a_block():
    assert "429" in detect_block(429, None, OK_URL)

def test_feedback_required_is_a_block():
    assert "feedback_required" in detect_block(400, {"message": "feedback_required"}, OK_URL)

def test_challenge_required_is_a_block():
    assert "challenge_required" in detect_block(400, {"message": "challenge_required"}, OK_URL)

def test_redirect_to_login_is_a_block():
    assert detect_block(200, None, "https://www.instagram.com/accounts/login/?next=/alice/")

def test_challenge_url_is_a_block():
    assert detect_block(200, None, "https://www.instagram.com/challenge/AbCd/")

def test_challenge_url_without_trailing_slash_is_a_block():
    assert detect_block(200, None, "https://www.instagram.com/challenge?next=/alice/")

def test_nested_feedback_required_is_found():
    assert detect_block(200, {"data": {"error": {"message": "feedback_required"}}}, OK_URL)


# --- is_bare_login_redirect: the narrow signal --login is allowed to suppress
#
# Pure boundary tests for the helper browser.Session.expect_login_redirect
# relies on. Kept separate from detect_block's own tests above because this
# function must stay *stricter* than detect_block, not an alias for one of its
# branches -- every case here that returns False is a case detect_block would
# also flag, and that flag must survive even during a declared login
# navigation.

def test_is_bare_login_redirect_true_for_a_plain_arrival():
    assert is_bare_login_redirect(200, None, LOGIN_URL) is True

def test_is_bare_login_redirect_false_on_401():
    assert is_bare_login_redirect(401, None, LOGIN_URL) is False

def test_is_bare_login_redirect_false_on_429():
    assert is_bare_login_redirect(429, None, LOGIN_URL) is False

def test_is_bare_login_redirect_false_with_challenge_required_payload():
    assert is_bare_login_redirect(200, {"message": "challenge_required"}, LOGIN_URL) is False

def test_is_bare_login_redirect_false_with_feedback_required_payload():
    assert is_bare_login_redirect(200, {"message": "feedback_required"}, LOGIN_URL) is False

def test_is_bare_login_redirect_false_with_nested_challenge_required_payload():
    assert is_bare_login_redirect(
        200, {"data": {"error": {"message": "challenge_required"}}}, LOGIN_URL
    ) is False

def test_is_bare_login_redirect_false_for_a_non_login_url():
    assert is_bare_login_redirect(200, None, OK_URL) is False

def test_is_bare_login_redirect_false_for_the_challenge_path():
    assert is_bare_login_redirect(200, None, "https://www.instagram.com/challenge/AbCd/") is False
