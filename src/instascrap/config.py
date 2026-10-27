"""Configuration, read from environment variables -- the Unraid template's form is
the only place a user ever edits it.

The public_prefix is a shared secret: it is the only thing standing between the
rehosted media and anyone who guesses the hostname. Left empty, it is generated
once and kept in the data directory, so a user who never touches it still gets a
stable, unguessable URL."""

import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PREFIX_RE = re.compile(r"^[0-9a-f]{16}$")
# Account names are concatenated straight into filesystem paths (posts/<account>/,
# media/<account>/) and into public URLs, so they are validated at the door rather
# than sanitised at every use site. This is Instagram's own username charset.
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SLOT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
FEED_SECRET_FILE = "feed_secret"
# RFB (VNC) authentication is DES-based and silently keeps only the first 8
# bytes of the password: anything past that adds nothing.
VNC_PASSWORD_MAX = 8

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """A configuration mistake, worded for the person filling in the template:
    every message names the variable at fault."""


@dataclass(frozen=True)
class Config:
    accounts: tuple[str, ...]
    data_dir: Path
    publish_dir: Path
    public_base_url: str
    public_prefix: str
    accounts_per_session: int
    feed_items: int
    min_gap_seconds: int
    max_gap_seconds: int
    # Defaulted so the many test fixtures that build a Config by hand for the
    # collection logic, which never reads these, stay valid.
    schedule: tuple[time, ...] = (time(8), time(14), time(21))
    jitter_minutes: int = 30
    vnc_password: str = ""
    discord_webhook: str | None = None
    host_hostname: str | None = None
    tz: str = "UTC"


def parse_accounts(raw: str) -> tuple[str, ...]:
    # Users paste handles as Instagram displays them ("@name"), with stray
    # spaces and trailing commas; none of that is worth a startup failure.
    # Duplicates would be visited twice per rotation, so they go too.
    accounts: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        name = item.strip().removeprefix("@")
        # Instagram usernames are case-insensitive, so NASAGoddard and
        # nasagoddard are one account. The first spelling is kept as typed:
        # archive paths and feed URLs already use it.
        if not name or name.lower() in seen:
            continue
        # `.` and `..` match the charset but would escape or collapse the archive
        # tree; no real Instagram username looks like that anyway.
        if not ACCOUNT_RE.match(name) or name in (".", ".."):
            raise ConfigError(f"ACCOUNTS: invalid Instagram username {name!r}")
        accounts.append(name)
        seen.add(name.lower())
    if not accounts:
        raise ConfigError("ACCOUNTS: at least one Instagram username is required")
    return tuple(accounts)


def parse_schedule(raw: str) -> tuple[time, ...]:
    slots: set[time] = set()
    for item in raw.split(","):
        # Same leniency as ACCOUNTS: a trailing comma is a typo, not an error.
        if not item.strip():
            continue
        match = SLOT_RE.match(item.strip())
        if not match:
            raise ConfigError(
                f"SCHEDULE: {item.strip()!r} is not a HH:MM time; "
                "expected a comma-separated list such as 08:00,14:00,21:00")
        slots.add(time(int(match.group(1)), int(match.group(2))))
    if not slots:
        raise ConfigError("SCHEDULE: at least one HH:MM time is required, e.g. 08:00,14:00,21:00")
    return tuple(sorted(slots))


def resolve_feed_secret(value: str, data_dir: Path) -> str:
    value = value.strip()
    if value:
        if not PREFIX_RE.match(value):
            raise ConfigError("FEED_SECRET: must be 16 lowercase hexadecimal characters, "
                              "or empty to generate one")
        return value

    path = data_dir / FEED_SECRET_FILE
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if PREFIX_RE.match(stored):
            return stored
        # A hand-edited or truncated file must not silently rotate every public
        # URL: subscribers would lose their feeds without any explanation.
        raise ConfigError(f"FEED_SECRET: {path} does not hold 16 hex characters; "
                          "fix or delete it")

    generated = secrets.token_hex(8)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(generated + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return generated


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name}: required")
    return value


def _integer(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}: {raw!r} is not a whole number") from None
    if value < minimum:
        raise ConfigError(f"{name}: must be at least {minimum}")
    return value


def _public_url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"PUBLIC_URL: {raw!r} must start with http:// or https:// "
                          "followed by a host, e.g. https://feeds.example.com")
    return raw.rstrip("/")


def load_config(env: Mapping[str, str], data_dir: Path = Path("/data")) -> Config:
    accounts = parse_accounts(_required(env, "ACCOUNTS"))
    public_base_url = _public_url(_required(env, "PUBLIC_URL"))

    vnc_password = _required(env, "VNC_PASSWORD")
    # x11vnc reads the password from a -passwdfile, where "#" lines are
    # comments and "__...__" lines are directives (__EMPTY__, __BEGIN_VIEWONLY__):
    # either could leave the login screen with no password at all.
    if vnc_password.startswith(("#", "__")) or "\n" in vnc_password or "\r" in vnc_password:
        raise ConfigError("VNC_PASSWORD: must not start with '#' or '__' "
                          "or contain a line break")
    if len(vnc_password) > VNC_PASSWORD_MAX:
        log.warning("VNC_PASSWORD: VNC authentication uses only the first 8 characters "
                    "of the password; the rest is ignored")

    tz = env.get("TZ", "").strip() or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TZ: unknown time zone {tz!r}") from None

    min_gap = _integer(env, "MIN_GAP_SECONDS", 25, 0)
    max_gap = _integer(env, "MAX_GAP_SECONDS", 90, 0)
    if min_gap > max_gap:
        raise ConfigError("MIN_GAP_SECONDS: must not exceed MAX_GAP_SECONDS")

    return Config(
        accounts=accounts,
        data_dir=data_dir,
        publish_dir=data_dir / "public",
        public_base_url=public_base_url,
        public_prefix=resolve_feed_secret(env.get("FEED_SECRET", ""), data_dir),
        accounts_per_session=_integer(env, "ACCOUNTS_PER_SESSION", 7, 1),
        feed_items=_integer(env, "FEED_ITEMS", 50, 1),
        min_gap_seconds=min_gap,
        max_gap_seconds=max_gap,
        schedule=parse_schedule(env.get("SCHEDULE", "").strip() or "08:00,14:00,21:00"),
        jitter_minutes=_integer(env, "JITTER_MINUTES", 30, 0),
        vnc_password=vnc_password,
        discord_webhook=env.get("DISCORD_WEBHOOK", "").strip() or None,
        host_hostname=env.get("HOST_HOSTNAME", "").strip() or None,
        tz=tz,
    )
