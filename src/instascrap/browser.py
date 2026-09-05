"""The only module that talks to a browser.

Everything Instagram-facing goes through a real Chrome on a persistent profile:
the TLS and HTTP/2 fingerprints, the JS-generated headers and the rotating
x-ig-www-claim all come for free that way, which is exactly what a Python HTTP
client cannot reproduce."""

import logging
import random
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import Config
from .parse import detect_block

log = logging.getLogger(__name__)

API_MARKERS = ("/api/v1/", "/graphql/query")


class BlockedError(RuntimeError):
    """Instagram pushed back. Never retried — the session ends here."""


class MediaFetchError(RuntimeError):
    """A media download failed for an ordinary reason (CDN hiccup, 404, 5xx).

    Distinct from BlockedError on purpose: this one is isolated and skipped by
    the orchestrator, whereas a BlockedError ends the whole session."""


def collect_payload(status: int, url: str, payload: dict | None) -> dict | None:
    """Decide what to do with one intercepted response. Pure, hence testable."""
    reason = detect_block(status, payload, url)
    if reason:
        raise BlockedError(reason)
    if payload is None or not any(marker in url for marker in API_MARKERS):
        return None
    return payload


def is_transient_navigation_error(exc: Exception) -> bool:
    """True only when no HTTP response was ever received for a navigation: a
    Playwright timeout, or a transport-level `net::ERR_*` surfaced as a plain
    Playwright Error. In both cases `detect_block` never ran against anything
    — there was no response to examine — so nothing was refused, which is
    exactly what makes a retry safe.

    A `BlockedError` is neither of these: it is a RuntimeError, not a
    PlaywrightError subclass, so it always falls through to False here no
    matter what its message says. That is deliberate, not incidental — it is
    what keeps a refusal from ever being retried."""
    if isinstance(exc, PlaywrightTimeoutError):
        return True
    return isinstance(exc, PlaywrightError) and "net::ERR_" in str(exc)


def _retry_pause() -> None:
    # Seconds, not the 25-90s inter-profile gap: this covers a startup race
    # (Xvfb/x11vnc/websockify binding sockets, Chrome's own network stack
    # coming up) while the session's first navigation is already in flight —
    # not a courtesy pause to Instagram, so it stays short.
    time.sleep(random.uniform(1.0, 3.0))


NAV_RETRY_ATTEMPTS = 3  # at most two retries, three attempts total


def retry_transient(fn, is_transient, *, attempts: int = NAV_RETRY_ATTEMPTS, sleep=_retry_pause, on_retry=None):
    """Call `fn()` up to `attempts` times total, retrying only exceptions that
    `is_transient` accepts. Anything `is_transient` rejects — most importantly
    a `BlockedError`, which no classifier here ever accepts — propagates from
    whichever attempt raised it, with no sleep and no further attempts. That
    is the one property the whole project depends on, so it is enforced here
    once rather than in every call site.

    `fn` and `sleep` are 0-arg callables and `is_transient` takes the caught
    exception; all three are injected so this stays pure and so tests never
    burn wall-clock time or touch a browser."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — classified by is_transient, not swallowed
            if not is_transient(exc) or attempt == attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            sleep()


class Session:
    def __init__(self, cfg: Config, profile_dir: Path, headless: bool = True):
        self.cfg = cfg
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self._captured: list[dict] = []
        self._block: BlockedError | None = None

    def __enter__(self) -> "Session":
        # The `with` protocol never calls __exit__ if __enter__ itself raises, so a
        # failure partway through setup (bad channel, locked profile dir, launch
        # timeout) must clean up whatever was already started before propagating —
        # otherwise the driver subprocess is orphaned for the life of the process.
        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=self.headless,
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1440, "height": 900},
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.on("response", self._on_response)
        except Exception:
            self._playwright.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.context.close()
        self._playwright.stop()

    def _on_response(self, response) -> None:
        # Playwright swallows exceptions raised inside event handlers, so a block
        # is stashed here and re-raised from visit_profile instead.
        payload = None
        if "application/json" in (response.headers.get("content-type") or ""):
            try:
                payload = response.json()
            except Exception:
                payload = None
        try:
            captured = collect_payload(response.status, response.url, payload)
        except BlockedError as exc:
            self._block = exc
            return
        if captured is not None:
            self._captured.append(captured)

    def _raise_if_blocked(self) -> None:
        """Checked at every boundary of every phase, not just at the end of a
        navigation. Responses keep arriving during the media-fetch phase, which
        can run for tens of seconds, and a block that lands there must never be
        carried silently into the next profile."""
        if self._block is not None:
            raise self._block

    def _pause(self, low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    def goto(self, url: str, **kwargs) -> None:
        """Navigate with a bounded retry for transport-level failures only.

        Deliberately used only for the session's *first* navigation
        (warm_up's goto, and the --login path in main.py) — the race this
        exists for is Chrome's first navigation of a session against
        Xvfb/x11vnc/websockify still binding sockets right after container
        start, observed in production as ERR_NETWORK_CHANGED and as a
        domcontentloaded that never fires within the 30s default timeout.
        There is no evidence of that race recurring mid-session, and
        visit_profile() already has a graceful path for an ordinary
        navigation failure — the account is logged, marked visited so the
        rotation still advances, and the session moves on — so retrying
        there would spend up to ~90s of extra requests against Instagram
        buying protection against a failure mode nothing points at. Do not
        widen this to visit_profile() without new evidence of the race
        happening there.

        Retrying is safe here specifically because a timeout or net::ERR_*
        means no HTTP response ever arrived: detect_block had nothing to
        examine, so Instagram never got the chance to refuse anything.

        A stashed block always wins over a transient error, on every attempt:
        each attempt starts by checking for one, so a block that arrived
        during a previous failed attempt aborts the retry instead of
        navigating again; and if the error that finally propagates out of
        retry_transient is a transient one, the block is checked once more
        before letting it through, in case it arrived during that very last
        attempt (which never gets a next attempt to catch it at the top)."""
        def attempt() -> None:
            self._raise_if_blocked()
            self.page.goto(url, **kwargs)

        def on_retry(attempt_no: int, exc: Exception) -> None:
            log.warning("navigation to %s failed (attempt %d/%d): %s — retrying",
                        url, attempt_no, NAV_RETRY_ATTEMPTS, exc)

        try:
            retry_transient(attempt, is_transient_navigation_error,
                             attempts=NAV_RETRY_ATTEMPTS, on_retry=on_retry)
        except BlockedError:
            raise
        except Exception:  # noqa: BLE001 — re-raised as-is unless a block supersedes it
            self._raise_if_blocked()
            raise

    def warm_up(self) -> None:
        """Land on the home feed and scroll a little before touching any profile.
        Costs nothing and makes the session read as a visit rather than a sweep."""
        # The only place _block is ever cleared. A block is a property of the
        # session, not of one navigation: clearing it on entry to visit_profile
        # (as this used to) threw away a 429 that arrived while the previous
        # profile's media were downloading, and then navigated anyway.
        self._block = None
        self.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        for _ in range(random.randint(2, 3)):
            self._pause(2, 6)
            self.page.mouse.wheel(0, random.randint(600, 1200))
        self._pause(2, 6)
        self._raise_if_blocked()

    def visit_profile(self, account: str) -> list[dict]:
        self._raise_if_blocked()
        self._captured = []
        # Single attempt, deliberately: the startup race goto() retries for is
        # specific to a session's first navigation, and a mid-session failure
        # here already has a graceful path (see _visit_account in main.py) —
        # the account is skipped and marked visited, and the rotation moves
        # on. Retrying up to ~90s here would trade that cheap fallback for
        # extra requests against Instagram with no evidence the race recurs
        # mid-session.
        self.page.goto(f"https://www.instagram.com/{account}/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(random.randint(1500, 3500))
        self._raise_if_blocked()
        return list(self._captured)

    def fetch_media(self, url: str) -> bytes:
        self._raise_if_blocked()
        # Through the browser context, so the request carries its cookies and headers.
        # APIRequestContext traffic never reaches page.on("response"), so media —
        # most of a session by request count — would otherwise bypass block
        # detection entirely. The status is run through detect_block by hand.
        response = self.context.request.get(url)
        reason = detect_block(response.status, None, response.url)
        if reason:
            raise BlockedError(reason)
        if response.status != 200:
            # APIResponse.body() does not raise on a non-2xx: it hands back the
            # error page. Writing that to disk would produce a permanently broken
            # 0.jpg, because save_post never rewrites a file that already exists.
            raise MediaFetchError(f"HTTP {response.status} fetching {url}")
        body = response.body()
        self._raise_if_blocked()
        return body

    def gap(self) -> None:
        self._raise_if_blocked()
        self._pause(self.cfg.min_gap_seconds, self.cfg.max_gap_seconds)
        self._raise_if_blocked()
