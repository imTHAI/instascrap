"""Configuration loading. The public_prefix is a shared secret: it is the only
thing standing between the rehosted media and anyone who guesses the hostname."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PREFIX_RE = re.compile(r"^[0-9a-f]{16}$")


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


def load_config(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())

    accounts = tuple(raw["accounts"])
    if not accounts:
        raise ValueError("accounts must not be empty")

    prefix = raw["public_prefix"]
    if not PREFIX_RE.match(prefix):
        raise ValueError("public_prefix must be 16 lowercase hex characters")

    if raw["min_gap_seconds"] > raw["max_gap_seconds"]:
        raise ValueError("min_gap_seconds must not exceed max_gap_seconds")

    return Config(
        accounts=accounts,
        data_dir=Path(raw["data_dir"]),
        publish_dir=Path(raw["publish_dir"]),
        public_base_url=raw["public_base_url"].rstrip("/"),
        public_prefix=prefix,
        accounts_per_session=int(raw["accounts_per_session"]),
        feed_items=int(raw["feed_items"]),
        min_gap_seconds=int(raw["min_gap_seconds"]),
        max_gap_seconds=int(raw["max_gap_seconds"]),
    )
