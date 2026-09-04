from instascrap.parse import detect_block

OK_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username=alice"

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

def test_nested_feedback_required_is_found():
    assert detect_block(200, {"data": {"error": {"message": "feedback_required"}}}, OK_URL)
