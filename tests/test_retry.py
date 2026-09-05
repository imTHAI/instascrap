"""The retry policy is pure and injectable by design (see browser.py's
Session.goto docstring): Session itself stays untested per the project's
convention (launching a real browser is out of scope for unit tests), but the
policy it relies on is a plain function of a callable and a classifier, so
it is fully testable without Playwright."""

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
import pytest

from instascrap.browser import BlockedError, is_transient_navigation_error, retry_transient


# --- is_transient_navigation_error -------------------------------------------

def test_timeout_error_is_transient():
    assert is_transient_navigation_error(PlaywrightTimeoutError("Timeout 30000ms exceeded")) is True


def test_net_err_is_transient():
    assert is_transient_navigation_error(PlaywrightError("net::ERR_NETWORK_CHANGED at https://x")) is True


def test_plain_playwright_error_without_net_err_is_not_transient():
    assert is_transient_navigation_error(PlaywrightError("something else went wrong")) is False


def test_blocked_error_is_never_transient_even_with_net_err_in_message():
    # BlockedError is a RuntimeError, not a PlaywrightError — the classifier
    # must reject it on type alone, never on message content, since a refusal
    # must never be retried no matter what its text happens to contain.
    assert is_transient_navigation_error(BlockedError("net::ERR_ looks transient but is not")) is False


def test_unrelated_exception_is_not_transient():
    assert is_transient_navigation_error(ValueError("boom")) is False


# --- retry_transient ----------------------------------------------------------

class Countdown:
    """A callable that fails a fixed number of times before succeeding."""
    def __init__(self, fail_times: int, exc_factory=lambda: PlaywrightTimeoutError("timeout")):
        self.fail_times = fail_times
        self.calls = 0
        self.exc_factory = exc_factory

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return "ok"


def test_transient_failure_then_success():
    fn = Countdown(fail_times=1)
    sleeps = []
    retries = []
    result = retry_transient(
        fn, is_transient_navigation_error,
        sleep=lambda: sleeps.append(1),
        on_retry=lambda n, exc: retries.append((n, exc)),
    )
    assert result == "ok"
    assert fn.calls == 2
    assert len(sleeps) == 1
    assert retries == [(1, retries[0][1])]
    assert isinstance(retries[0][1], PlaywrightTimeoutError)


def test_exhausts_all_attempts_and_propagates_the_last_error():
    fn = Countdown(fail_times=99)  # always fails
    sleeps = []
    with pytest.raises(PlaywrightTimeoutError):
        retry_transient(fn, is_transient_navigation_error, attempts=3, sleep=lambda: sleeps.append(1))
    assert fn.calls == 3
    # Sleeps only happen between attempts: 3 attempts -> 2 sleeps, none after
    # the final, unrecoverable failure.
    assert len(sleeps) == 2


def test_blocked_error_propagates_on_first_attempt_with_no_retry():
    calls = []

    def fn():
        calls.append(1)
        raise BlockedError("429")

    sleeps = []
    with pytest.raises(BlockedError, match="429"):
        retry_transient(fn, is_transient_navigation_error, sleep=lambda: sleeps.append(1))
    assert calls == [1]
    assert sleeps == []


def test_non_transport_error_propagates_unretried():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("not a transport failure")

    sleeps = []
    with pytest.raises(ValueError, match="not a transport failure"):
        retry_transient(fn, is_transient_navigation_error, sleep=lambda: sleeps.append(1))
    assert calls == [1]
    assert sleeps == []


def test_on_retry_receives_attempt_number_and_exception():
    fn = Countdown(fail_times=2)
    seen = []
    retry_transient(fn, is_transient_navigation_error, sleep=lambda: None,
                     on_retry=lambda n, exc: seen.append(n))
    assert seen == [1, 2]
