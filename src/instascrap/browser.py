"""The only module that talks to a browser.

Everything Instagram-facing goes through a real Chrome on a persistent profile:
the TLS and HTTP/2 fingerprints, the JS-generated headers and the rotating
x-ig-www-claim all come for free that way, which is exactly what a Python HTTP
client cannot reproduce."""

import logging
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import Config
from .parse import detect_block

log = logging.getLogger(__name__)

API_MARKERS = ("/api/v1/", "/graphql/query")


class BlockedError(RuntimeError):
    """Instagram pushed back. Never retried — the session ends here."""


def collect_payload(status: int, url: str, payload: dict | None) -> dict | None:
    """Decide what to do with one intercepted response. Pure, hence testable."""
    reason = detect_block(status, payload, url)
    if reason:
        raise BlockedError(reason)
    if payload is None or not any(marker in url for marker in API_MARKERS):
        return None
    return payload


class Session:
    def __init__(self, cfg: Config, profile_dir: Path, headless: bool = True):
        self.cfg = cfg
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self._captured: list[dict] = []
        self._block: BlockedError | None = None

    def __enter__(self) -> "Session":
        self._playwright = sync_playwright().start()
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

    def _pause(self, low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    def warm_up(self) -> None:
        """Land on the home feed and scroll a little before touching any profile.
        Costs nothing and makes the session read as a visit rather than a sweep."""
        self._block = None
        self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        for _ in range(random.randint(2, 3)):
            self._pause(2, 6)
            self.page.mouse.wheel(0, random.randint(600, 1200))
        self._pause(2, 6)
        if self._block:
            raise self._block

    def visit_profile(self, account: str) -> list[dict]:
        self._captured = []
        self._block = None
        self.page.goto(f"https://www.instagram.com/{account}/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(random.randint(1500, 3500))
        if self._block:
            raise self._block
        return list(self._captured)

    def fetch_media(self, url: str) -> bytes:
        # Through the browser context, so the request carries its cookies and headers.
        return self.context.request.get(url).body()

    def gap(self) -> None:
        self._pause(self.cfg.min_gap_seconds, self.cfg.max_gap_seconds)
