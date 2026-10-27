import contextlib
from datetime import datetime, timedelta, timezone

from instascrap.browser import BlockedError
from instascrap.config import Config
from instascrap.login import is_logged_in, run_login, vnc_url
from instascrap.store import Store

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def cfg_with(tmp_path, host_hostname="Tower"):
    return Config(accounts=("alice",), data_dir=tmp_path, publish_dir=tmp_path / "public",
                  public_base_url="https://example.test", public_prefix="0123456789abcdef",
                  accounts_per_session=1, feed_items=50, min_gap_seconds=0, max_gap_seconds=0,
                  vnc_password="secret12", host_hostname=host_hostname)


class FakeScreen:
    def __init__(self, password):
        self.password = password

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_session_states(states):
    """A Session stand-in: each raise_if_blocked() call moves to the next state.
    A state is True (logged in), False (still on the login page) or an exception
    to raise from raise_if_blocked."""
    class FakeSession:
        def __init__(self, cfg, profile_dir, headless=True):
            assert headless is False
            self.index = -1
            self.gotos = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @contextlib.contextmanager
        def login_mode(self):
            yield

        def goto(self, url, **kwargs):
            self.gotos.append(url)

        def ensure_page(self):
            return False

        def raise_if_blocked(self):
            self.index = min(self.index + 1, len(states) - 1)
            if isinstance(states[self.index], Exception):
                raise states[self.index]

        def cookie_names(self):
            return {"sessionid", "ds_user_id"} if states[self.index] is True else {"csrftoken"}

        def current_url(self):
            return "https://www.instagram.com/"
    return FakeSession


def recording_alerter(sent):
    class Recorder:
        def send(self, level, title, message):
            sent.append((level, title))
    return Recorder()


def test_is_logged_in():
    full = {"sessionid", "ds_user_id", "csrftoken"}
    assert is_logged_in(full, "https://www.instagram.com/")
    assert not is_logged_in({"csrftoken"}, "https://www.instagram.com/")
    assert not is_logged_in(full, "https://www.instagram.com/challenge/x/")
    assert not is_logged_in(full, "https://www.instagram.com/accounts/login/two_factor/")


def test_vnc_url_uses_unraid_hostname(tmp_path):
    assert vnc_url(cfg_with(tmp_path, host_hostname="Tower")) == "http://Tower:6080/vnc.html"
    assert "<your-server-ip>" in vnc_url(cfg_with(tmp_path, host_hostname=None))


def test_login_success_clears_state_and_announces(tmp_path):
    store, alerts = Store(tmp_path), []
    store.require_login("first run", NOW)
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([False, False, True]))
    assert store.login_required() is None
    assert [t for _, t in alerts] == ["Instagram login needed", "Logged in"]


def test_already_valid_session_sends_no_login_alert(tmp_path):
    store, alerts = Store(tmp_path), []
    store.require_login("manual login", NOW)
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([True]))
    assert [t for _, t in alerts] == ["Logged in"]


def test_reminder_once_a_day(tmp_path):
    store, alerts = Store(tmp_path), []
    store.require_login("expired", NOW)
    ticks = iter(NOW + timedelta(hours=13 * i) for i in range(20))
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: next(ticks),
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([False] * 6 + [True]))
    # Observed not logged in at t=13h, 26h, ..., 78h: alerted at 13h, then 39h
    # and 65h, each at least 24h after the previous alert.
    assert [t for _, t in alerts].count("Instagram login needed") == 3


def test_never_opens_during_backoff(tmp_path):
    store = Store(tmp_path)
    store.require_login("x", NOW)
    store.record_block("HTTP 429", NOW)
    opened = []
    run_login(cfg_with(tmp_path), store, recording_alerter([]), clock=lambda: NOW,
              sleep=lambda s: None,
              screen_factory=lambda pw: opened.append(pw) or FakeScreen(pw),
              session_factory=fake_session_states([True]))
    assert opened == []


def test_block_during_login_records_backoff_and_keeps_login_required(tmp_path):
    store, alerts = Store(tmp_path), []
    store.require_login("expired", NOW)
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([False, BlockedError("challenge_required")]))
    assert store.blocked_until() is not None and store.login_required() == "expired"
    assert ("alert", "Instagram is throttling") in alerts


def test_screen_gets_the_configured_password(tmp_path):
    store, seen = Store(tmp_path), []
    store.require_login("expired", NOW)
    run_login(cfg_with(tmp_path), store, recording_alerter([]), clock=lambda: NOW,
              sleep=lambda s: None,
              screen_factory=lambda pw: seen.append(pw) or FakeScreen(pw),
              session_factory=fake_session_states([True]))
    assert seen == ["secret12"]


def test_block_on_the_same_poll_as_logged_in_is_not_lost(tmp_path):
    # Instagram's post-login burst can carry a throttle that is only processed
    # during the final cookie read: it must still end in a backoff.
    store, alerts = Store(tmp_path), []
    store.require_login("expired", NOW)

    class BlockLandsWithLogin(fake_session_states([False, True])):
        def cookie_names(self):
            names = super().cookie_names()
            if "sessionid" in names:
                self.late_block = BlockedError("HTTP 429 on /api/graphql")
            return names

        def raise_if_blocked(self):
            if getattr(self, "late_block", None):
                raise self.late_block
            super().raise_if_blocked()

    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen, session_factory=BlockLandsWithLogin)
    assert store.blocked_until() is not None
    assert store.login_required() == "expired"
    assert ("info", "Logged in") not in alerts


def test_two_factor_code_entry_is_not_logged_in():
    cookies = {"sessionid", "ds_user_id"}
    assert not is_logged_in(cookies, "https://www.instagram.com/auth_platform/codeentry/?apc=x")
    assert not is_logged_in({"sessionid"}, "https://www.instagram.com/")
    assert is_logged_in(cookies, "https://www.instagram.com/")


def test_new_login_screen_within_a_day_does_not_realert(tmp_path):
    # The daemon retries a failed login screen hourly; each retry must not
    # send "login needed" again.
    store, alerts = Store(tmp_path), []
    store.require_login("expired", NOW)
    try:
        run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: NOW,
                  sleep=lambda s: None, screen_factory=FakeScreen,
                  session_factory=fake_session_states([False, RuntimeError("chrome died")]))
    except RuntimeError:
        pass
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts),
              clock=lambda: NOW + timedelta(hours=1), sleep=lambda s: None,
              screen_factory=FakeScreen, session_factory=fake_session_states([False, True]))
    assert [t for _, t in alerts].count("Instagram login needed") == 1


def test_closed_chrome_window_is_reopened_on_the_login_page(tmp_path):
    store = Store(tmp_path)
    store.require_login("expired", NOW)
    sessions = []

    class ClosesOnce(fake_session_states([False, False, True])):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.reopened = False
            sessions.append(self)

        def ensure_page(self):
            if self.index == 0 and not self.reopened:
                self.reopened = True
                return True
            return False

    run_login(cfg_with(tmp_path), store, recording_alerter([]), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen, session_factory=ClosesOnce)
    assert sessions[0].gotos == ["https://www.instagram.com/accounts/login/"] * 2
    assert store.login_required() is None
