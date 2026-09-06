"""The only module that talks to a browser.

Everything Instagram-facing goes through a real Chrome on a persistent profile:
the TLS and HTTP/2 fingerprints, the JS-generated headers and the rotating
x-ig-www-claim all come for free that way, which is exactly what a Python HTTP
client cannot reproduce."""

import contextlib
import logging
import random
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import Config
from .parse import detect_block, has_profile_grid, is_bare_login_redirect

log = logging.getLogger(__name__)

API_MARKERS = ("/api/v1/", "/graphql/query")

# How long visit_profile waits for the response carrying the profile grid,
# instead of the flat 1.5-3.5s sleep this replaces (see visit_profile's
# docstring for the production failure that sleep caused). 30s is not a round
# number picked in isolation: it is Playwright's own default timeout for a
# page action, already the standing tolerance for this exact navigation's
# domcontentloaded wait (see is_transient_navigation_error's docstring, which
# names the same 30s default) -- so this waits, at most, exactly as long as
# the browser itself would already wait for the page to finish loading, no
# longer. In the overwhelmingly common case the grid response arrives within
# a second or two and PROFILE_GRID_POLL_S below lets the wait return early;
# the full 30s is only ever spent on a profile that is *already* an anomaly,
# where a few extra seconds of harmless polling is a fine trade against
# silently missing a profile's entire grid.
PROFILE_GRID_WAIT_TIMEOUT_S = 30.0
PROFILE_GRID_POLL_S = 0.25

# How long warm_up waits, after its scrolling ends, for the page to go quiet
# before handing control to the first profile visit -- see warm_up's
# docstring for the production failure (michellemonaghan, three sessions)
# this closes. Playwright's own networkidle wait was rejected on purpose:
# Instagram's home feed keeps polling in the background (unread counts,
# presence) for as long as the tab is open, so "zero in-flight requests"
# can simply never occur and networkidle would turn this into an unbounded
# hang -- exactly the failure mode a one-shot cron session cannot afford.
# WARM_UP_SETTLE_QUIET_S is instead a *quiet window*: this many seconds
# with no new response event at all, of any kind, is treated as "the home
# feed's own traffic has caught up" without requiring it to ever fully
# stop. WARM_UP_SETTLE_TIMEOUT_S is the backstop for when that quiet window
# never arrives -- continuous background chatter, a slow connection -- so
# the session still moves on rather than stalling on Instagram's own noise.
# Both are deliberately small next to a ~10-minute session: the failure
# this fixes was a home-feed response arriving a beat late, not minutes
# late, so a few seconds of margin is what production evidence calls for.
WARM_UP_SETTLE_QUIET_S = 1.5
WARM_UP_SETTLE_TIMEOUT_S = 8.0
WARM_UP_SETTLE_POLL_S = 0.25


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


def wait_for_condition(is_met, is_aborted, *, timeout_s: float, poll_s: float) -> bool:
    """Poll `is_met` every `poll_s` until it turns true, `is_aborted` fires, or
    `timeout_s` has elapsed; returns whether `is_met` was the reason.

    `is_aborted` is checked first on every iteration, including the first —
    an abort already true when the wait starts must win before `is_met` is
    even consulted, which is what lets a caller use this to check a stashed
    block without a race against the very first poll.

    Elapsed time is a running tally of the `poll_s` increments actually
    slept, not a wall-clock read: this is what makes it possible to unit-test
    the full-timeout path instantly under a mocked `time.sleep`, mirroring
    the pattern `retry_transient` above already uses for the same reason."""
    elapsed = 0.0
    while True:
        if is_aborted():
            return False
        if is_met():
            return True
        if elapsed >= timeout_s:
            return False
        time.sleep(poll_s)
        elapsed += poll_s


class Session:
    def __init__(self, cfg: Config, profile_dir: Path, headless: bool = True):
        self.cfg = cfg
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self._captured: list[dict] = []
        self._block: BlockedError | None = None
        # Incremented on every response event, regardless of what it carries
        # or whether it was ignored -- warm_up's settle wait (below) watches
        # this to notice "the page has gone quiet" without caring which
        # request that was, or ever inspecting _captured itself.
        self._response_count = 0
        # Off by default and never touched by --run's path -- only
        # expect_login_redirect() below ever sets this, and only for the
        # single navigation it wraps. See that method's docstring.
        self._expect_login_redirect = False

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
        # Counted first, before any of the classification below -- a blocked
        # or ignored response is still activity as far as warm_up's settle
        # wait cares, and it must never be skipped by an early return.
        self._response_count += 1
        payload = None
        if "application/json" in (response.headers.get("content-type") or ""):
            try:
                payload = response.json()
            except Exception:
                payload = None
        try:
            captured = collect_payload(response.status, response.url, payload)
        except BlockedError as exc:
            # is_bare_login_redirect re-checks the same status/payload/url the
            # exception was raised from, on purpose: the flag alone is not
            # enough to swallow this, because a 401 or a challenge_required
            # arriving on this very response during the declared navigation
            # must still be a block. Only the plain, signal-free arrival at
            # the login page is dropped.
            if self._expect_login_redirect and is_bare_login_redirect(
                response.status, payload, response.url
            ):
                return
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

    def raise_if_blocked(self) -> None:
        """Public counterpart to `_raise_if_blocked`, for the one caller
        outside this class that needs it: `--login` in main.py, which has to
        check for a block after the human finishes typing at a prompt this
        class has no method of its own for. Every boundary Session already
        knows about (navigation, media fetch, the inter-profile gap) checks
        itself; this exists for the boundary only the caller can name."""
        self._raise_if_blocked()

    def _pause(self, low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    @contextlib.contextmanager
    def expect_login_redirect(self):
        """Declares that the one navigation made inside this `with` block is
        *meant* to land on Instagram's login page -- `--login` mode's entire
        purpose, run by a human in a VNC session to create or refresh the
        persistent profile. Nothing else may use this: `--run`'s path never
        calls it, so the flag this sets defaults to (and everywhere else
        stays) False, and a redirect to the login page mid-session is still
        an immediate, unconditional block.

        Scoped to exactly the wrapped navigation, not to the session: the
        `finally` clears the flag whether the navigation succeeded, raised a
        real block, or raised anything else, so suppression can never leak
        into whatever runs after this `with` block -- in particular the
        human typing their password afterwards, where a `challenge_required`
        must still be caught like any other block. See
        `parse.is_bare_login_redirect` for the narrow signal this actually
        suppresses, and `goto`'s own final `_raise_if_blocked()` for why a
        real block picked up during this navigation still surfaces before
        this `with` block exits, even when it never made page.goto() itself
        raise."""
        self._expect_login_redirect = True
        try:
            yield
        finally:
            self._expect_login_redirect = False

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
        attempt (which never gets a next attempt to catch it at the top).

        And a block is checked once more even when the attempt raises
        nothing at all: a response event can stash one (a 401 riding on an
        API call fired alongside the navigation, say) without page.goto()
        itself ever raising, since Playwright's own load-completion wait
        neither depends on nor is interrupted by that response. Without this
        final check, such a block would sit in self._block undetected until
        some later call happened to test for it — fine for warm_up, which
        checks again after its scrolls, but --login's navigation has nothing
        after it in that role. This is what makes expect_login_redirect's
        suppression the only thing standing between this check and a raise:
        it does not special-case the login path, it just always looks."""
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
        self._raise_if_blocked()

    def _wait_for_network_quiet(self, *, quiet_s: float, timeout_s: float, poll_s: float) -> bool:
        """Bounded settle: wait until `quiet_s` seconds pass with no new
        response event, capped overall at `timeout_s`. Returns whether the
        quiet window was actually reached (False on abort or timeout).

        Built on `wait_for_condition`, not a bespoke loop: that is what gets
        the abort-checked-before-met guarantee for free, on every poll,
        exactly like `visit_profile`'s grid wait already relies on -- a
        block landing mid-settle must stop this immediately, the same as it
        would mid-scroll or mid-wait.

        `is_met` closes over a snapshot of `_response_count` rather than a
        wall-clock timestamp: `wait_for_condition` already tracks elapsed
        time as a tally of the `poll_s` increments it actually slept (see
        its own docstring for why -- it is what lets the full-timeout path
        run instantly under a mocked `time.sleep`), and mirroring that here
        is what keeps this settle equally instant to unit-test rather than
        introducing a second, real-time clock into the same wait."""
        last_count = self._response_count
        quiet_elapsed = 0.0

        def is_met() -> bool:
            nonlocal last_count, quiet_elapsed
            current = self._response_count
            if current != last_count:
                last_count = current
                quiet_elapsed = 0.0
            else:
                quiet_elapsed += poll_s
            return quiet_elapsed >= quiet_s

        return wait_for_condition(
            is_met=is_met,
            is_aborted=lambda: self._block is not None,
            timeout_s=timeout_s,
            poll_s=poll_s,
        )

    def warm_up(self) -> None:
        """Land on the home feed, scroll a little, then wait for the page to
        go quiet before returning. Costs nothing and makes the session read
        as a visit rather than a sweep.

        The settle at the end is the fix for a real production failure:
        michellemonaghan, always the first profile visited after warm_up,
        came back three times with only a home-feed payload and never its
        own grid -- reproduced nowhere else, including a --capture run on
        the same profile with no warm_up before it. warm_up used to return
        the instant its scrolling paused, with the home feed's own request
        (fired by that very scrolling) possibly still in flight. The first
        profile's navigation then started on a page still busy with that
        request, and production evidence says the profile page did not
        reliably issue its own grid request in that state -- the late
        home-feed response was the only thing visit_profile's freshly
        cleared _captured buffer ever received. See
        _wait_for_network_quiet's docstring for the wait itself, and its
        module-level constants above for why a bounded quiet window was
        chosen over Playwright's own (unbounded) networkidle."""
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
        self._wait_for_network_quiet(
            quiet_s=WARM_UP_SETTLE_QUIET_S,
            timeout_s=WARM_UP_SETTLE_TIMEOUT_S,
            poll_s=WARM_UP_SETTLE_POLL_S,
        )
        # Checked again after the settle, for the same reason goto() and
        # visit_profile() check once more after their own waits: a block can
        # be stashed by a response event that lands during the settle's very
        # last poll_s sleep, after wait_for_condition's final is_aborted()
        # check has already run and before another iteration would ever look
        # again. Without this, such a block would sit in self._block unseen
        # until whatever runs next happened to check for it.
        self._raise_if_blocked()

    def _scroll_profile_grid(self) -> None:
        """A few modest wheel movements with short, irregular pauses -- long
        enough to read as a person glancing down a profile, short enough to
        stay a nudge rather than a browse. Movement size alone cannot promise
        a second grid page never loads -- see the early-exit note below for
        what actually guarantees that.

        Confirmed by hand against the production profile: a direct navigation
        often renders the first grid page from server-side data without ever
        issuing the GraphQL request this project intercepts -- no request, no
        capture, no posts, while the page itself looks perfectly normal.
        Scrolling is what makes that request fire; freezing for up to 30s on
        a page that never asked for anything is also not something a human
        does, so this replaces stillness with the same kind of motion
        warm_up already uses on the home feed, just smaller and shorter: that
        one is camouflage on a page nothing is extracted from, this one is
        the trigger for the one payload the whole visit exists to capture.

        Checked after every movement, not only before and after the whole
        sequence: a response can land in the middle of this exactly as it
        can during the grid wait right after it (Playwright keeps dispatching
        response events the same way regardless of what Python code is
        running), and a block found here must stop the session before
        another wheel event fires -- not be discovered only once scrolling
        has already run its course.

        Also stops the instant the grid itself shows up, for a reason that
        has nothing to do with speed: has_profile_grid cannot tell a first
        page of edges from a second one, so a movement made after the grid
        is already captured risks crossing Instagram's lazy-load threshold
        again and pulling in a page this project never asked for and does
        not want -- silently, and non-deterministically, contradicting the
        design's explicit "only the first view, ever" choice (see
        visit_profile's docstring). Widening what a visit collects is a
        decision for its own review, not a side effect of how many wheel
        events happened to still be queued up when the first response
        landed."""
        for _ in range(random.randint(2, 3)):
            if any(has_profile_grid(p) for p in self._captured):
                return
            self.page.mouse.wheel(0, random.randint(300, 600))
            self._pause(0.5, 1.5)
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
        # mid-session. This is still the one navigation of that single
        # attempt — only what happens *after* it changed.
        self.page.goto(f"https://www.instagram.com/{account}/", wait_until="domcontentloaded")
        self._raise_if_blocked()
        # Scroll before waiting, not interleaved with it or instead of it.
        # Scrolling is what triggers the grid request in the first place (see
        # _scroll_profile_grid's docstring), so it has to happen before there
        # is anything to wait for -- interleaving wheel events into the poll
        # loop below would mean the first several polls are guaranteed to
        # fail no matter how short PROFILE_GRID_POLL_S is, for no benefit.
        # And the bounded wait still runs afterwards unconditionally, rather
        # than being skipped when scrolling "should" have been enough: it is
        # the backstop for exactly the case scrolling cannot fix -- broken
        # interception, a reshaped page, a request that fires but is slow --
        # and folding it into the scroll loop would blur the one signal
        # (has_profile_grid never arriving) that grid_missing depends on.
        self._scroll_profile_grid()
        # Wait for the response that actually carries the grid, not a flat
        # delay: a flat 1.5-3.5s sleep is what let michellemonaghan's session
        # come back empty in production — the grid response hadn't arrived in
        # that window, so visit_profile returned with only a stray home-feed
        # payload left over from warm_up in `self._captured`, and nothing
        # downstream could tell that apart from an honest zero-new-posts
        # visit. Bounded by PROFILE_GRID_WAIT_TIMEOUT_S so a profile whose
        # grid genuinely never arrives (broken interception, a reshaped page)
        # can't stall the session — the caller detects that case from the
        # grid signal itself (has_profile_grid), not from the post count, so
        # it still fires even after this wait gives up.
        #
        # is_aborted checks the stashed block on every poll, not only once
        # this returns: a block landing mid-wait must stop the polling
        # immediately rather than being discovered only after 30s of waiting
        # on a session that is already over.
        wait_for_condition(
            is_met=lambda: any(has_profile_grid(p) for p in self._captured),
            is_aborted=lambda: self._block is not None,
            timeout_s=PROFILE_GRID_WAIT_TIMEOUT_S,
            poll_s=PROFILE_GRID_POLL_S,
        )
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
