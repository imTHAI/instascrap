"""Alerts, sent to a Discord webhook by the container itself.

Every alert is logged whether or not a webhook is configured: the container's
log is the one channel every install has. Sending is best effort -- a Discord
outage must never turn a finished run into a failed one."""

import json
import logging
import urllib.request
from datetime import datetime
from typing import Callable, Literal
from zoneinfo import ZoneInfo

Level = Literal["info", "warning", "alert"]

_COLORS = {"info": 0x2ECC71, "warning": 0xF1C40F, "alert": 0xE74C3C}
_TIMEOUT_S = 10

log = logging.getLogger(__name__)


def _post_json(url: str, body: dict) -> None:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        # Discord rejects urllib's default User-Agent with a 403.
        headers={"Content-Type": "application/json", "User-Agent": "instascrap"},
        method="POST")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S):
        pass


class Alerter:
    def __init__(self, webhook: str | None,
                 post: Callable[[str, dict], None] = _post_json):
        self.webhook = webhook
        self._post = post

    def send(self, level: Level, title: str, message: str) -> None:
        log.log(logging.INFO if level == "info" else logging.WARNING,
                "%s: %s", title, message)
        if not self.webhook:
            return
        body = {"embeds": [{"title": title, "description": message, "color": _COLORS[level]}]}
        try:
            self._post(self.webhook, body)
        except Exception as exc:  # noqa: BLE001 -- alerts must never fail the caller
            log.warning("Discord alert not delivered (%s): %s", title, exc)


def alert_blocked(alerter: Alerter, reason: str, until: datetime, tz: str) -> None:
    resume = until.astimezone(ZoneInfo(tz))
    alerter.send("alert", "Instagram is throttling",
                 f"{reason}. Pausing until {resume:%Y-%m-%d %H:%M %Z}.")
