"""On-disk archive and session state.

The archive is the source of truth; feeds are a disposable projection of it.
State writes go through a temp file and a rename, because a session can be killed
mid-write when the container is stopped."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

FIRST_BACKOFF = timedelta(hours=24)
REPEAT_BACKOFF = timedelta(hours=72)


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

    def _read(self) -> dict:
        if not self.state_path.exists():
            return {"visited": {}, "block": None, "last_new_post": None}
        return json.loads(self.state_path.read_text())

    def _write(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, self.state_path)

    # --- rotation -------------------------------------------------------

    def visit_order(self, accounts, n: int) -> list[str]:
        visited = self._read()["visited"]
        # Never-visited sorts first: the empty string precedes any ISO timestamp.
        return sorted(accounts, key=lambda a: visited.get(a, ""))[:n]

    def mark_visited(self, account: str, now: datetime) -> None:
        state = self._read()
        state["visited"][account] = now.isoformat()
        self._write(state)

    # --- blocking -------------------------------------------------------

    def blocked_until(self) -> datetime | None:
        block = self._read()["block"]
        return datetime.fromisoformat(block["until"]) if block else None

    def record_block(self, reason: str, now: datetime) -> datetime:
        state = self._read()
        backoff = REPEAT_BACKOFF if state["block"] else FIRST_BACKOFF
        until = now + backoff
        state["block"] = {"reason": reason, "at": now.isoformat(), "until": until.isoformat()}
        self._write(state)
        return until

    def clear_block(self) -> None:
        state = self._read()
        state["block"] = None
        self._write(state)

    # --- freshness watchdog ---------------------------------------------

    def last_new_post_at(self) -> datetime | None:
        value = self._read()["last_new_post"]
        return datetime.fromisoformat(value) if value else None

    def mark_new_post(self, now: datetime) -> None:
        state = self._read()
        state["last_new_post"] = now.isoformat()
        self._write(state)
