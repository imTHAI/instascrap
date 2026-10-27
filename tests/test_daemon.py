import logging
from datetime import datetime, time, timedelta, timezone

from instascrap.config import Config
from instascrap.daemon import CaddySupervisor, clear_profile_locks, initial_login_reason, serve
from instascrap.store import Store

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def cfg_with(tmp_path):
    return Config(accounts=("alice",), data_dir=tmp_path / "data",
                  publish_dir=tmp_path / "data" / "public",
                  public_base_url="https://example.test", public_prefix="0123456789abcdef",
                  accounts_per_session=1, feed_items=50, min_gap_seconds=0, max_gap_seconds=0,
                  schedule=(time(10),), jitter_minutes=0, vnc_password="secret12")


def with_profile(cfg):
    (cfg.data_dir / "profile" / "Default").mkdir(parents=True)
    return cfg


class FakeCaddy:
    def __init__(self, calls):
        self.calls = calls

    def start(self):
        self.calls.append("caddy.start")

    def poll(self):
        pass

    def stop(self):
        self.calls.append("caddy.stop")


def fakes(calls, now=NOW, login_clears=True):
    """A clock that only moves when the daemon sleeps, and recording stand-ins."""
    clock = {"now": now}

    def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)

    def login(cfg, store, alerter):
        calls.append("login")
        if login_clears:
            store.clear_login()

    def run(cfg, store, alerter, now):
        calls.append("run")

    return dict(clock=lambda: clock["now"], sleep=sleep, caddy=FakeCaddy(calls), login=login,
                run=run, publish=lambda store, cfg: calls.append("publish"))


def test_clear_profile_locks_removes_dangling_symlinks(tmp_path):
    (tmp_path / "SingletonLock").symlink_to("old-host-123")
    (tmp_path / "SingletonCookie").symlink_to("456")
    (tmp_path / "Default").mkdir()
    assert sorted(clear_profile_locks(tmp_path)) == ["SingletonCookie", "SingletonLock"]
    assert (tmp_path / "Default").exists()
    assert not (tmp_path / "SingletonLock").is_symlink()


def test_clear_profile_locks_on_missing_profile(tmp_path):
    assert clear_profile_locks(tmp_path / "nope") == []


def test_initial_login_reason(tmp_path):
    store = Store(tmp_path / "data")
    assert initial_login_reason(tmp_path / "missing", store) == "first run"
    (tmp_path / "empty").mkdir()
    assert initial_login_reason(tmp_path / "empty", store) == "first run"
    (tmp_path / "profile" / "Default").mkdir(parents=True)
    assert initial_login_reason(tmp_path / "profile", store) is None
    store.require_login("expired", NOW)
    assert initial_login_reason(tmp_path / "profile", store) == "expired"


def test_serve_publishes_before_first_run_and_logs_index(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    calls = []
    serve(with_profile(cfg_with(tmp_path)), **fakes(calls), max_iterations=1)
    assert calls[:3] == ["caddy.start", "publish", "run"]
    assert "Feeds index: https://example.test/0123456789abcdef/index.html" in caplog.text
    assert calls[-1] == "caddy.stop"


def test_serve_runs_at_the_scheduled_slot(tmp_path):
    calls, ran_at = [], []
    kw = fakes(calls)
    kw["run"] = lambda cfg, store, alerter, now: ran_at.append(now)
    serve(with_profile(cfg_with(tmp_path)), **kw, max_iterations=1)
    assert ran_at == [datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)]


def test_serve_logs_in_first_when_required(tmp_path):
    calls = []
    serve(cfg_with(tmp_path), **fakes(calls), max_iterations=2)   # empty profile: first run
    assert calls.index("login") < calls.index("run")


def test_serve_retries_login_hourly_while_unresolved(tmp_path):
    calls = []
    serve(cfg_with(tmp_path), **fakes(calls, login_clears=False), max_iterations=3)
    assert calls.count("login") == 3 and "run" not in calls


def test_serve_skips_slot_during_backoff(tmp_path, caplog):
    cfg = with_profile(cfg_with(tmp_path))
    Store(cfg.data_dir).record_block("HTTP 429", NOW)
    calls = []
    serve(cfg, **fakes(calls), max_iterations=1)
    assert "run" not in calls and "skipping" in caplog.text


def test_serve_survives_a_crashed_run(tmp_path):
    calls = []
    kw = fakes(calls)

    def crash(cfg, store, alerter, now):
        calls.append("run")
        raise OSError("disk full")

    kw["run"] = crash
    serve(with_profile(cfg_with(tmp_path)), **kw, max_iterations=2)
    assert calls.count("run") == 2


class FakePopen:
    def __init__(self, exit_code):
        self.exit_code = exit_code
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        pass


def test_caddy_supervisor_restarts_dead_process():
    procs = [FakePopen(exit_code=1), FakePopen(exit_code=None)]
    ticks = iter(range(0, 1000, 11))
    sup = CaddySupervisor(["caddy"], popen=lambda cmd: procs.pop(0), clock=lambda: next(ticks))
    sup.start()
    sup.poll()
    assert procs == []


def test_caddy_supervisor_does_not_restart_in_a_tight_loop():
    procs = [FakePopen(exit_code=1), FakePopen(exit_code=None)]
    sup = CaddySupervisor(["caddy"], popen=lambda cmd: procs.pop(0), clock=lambda: 0)
    sup.start()
    sup.poll()
    assert len(procs) == 1


def test_serve_holds_the_session_lock_during_runs_and_logins(tmp_path):
    import pytest
    from instascrap.lock import SessionBusy, session_lock
    cfg = cfg_with(tmp_path)
    seen = []

    def busy():
        try:
            with session_lock(cfg.data_dir, wait=False):
                return False
        except SessionBusy:
            return True

    kw = fakes([])
    kw["login"] = lambda c, store, alerter: (seen.append(("login", busy())), store.clear_login())
    kw["run"] = lambda c, store, alerter, now: seen.append(("run", busy()))
    serve(cfg, **kw, max_iterations=2)   # empty profile: login first, then a run
    assert seen == [("login", True), ("run", True)]
