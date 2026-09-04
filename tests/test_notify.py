import logging
from instascrap import notify as notify_mod


def test_returns_false_and_logs_when_unraid_script_absent(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(notify_mod, "NOTIFY_SCRIPT", tmp_path / "does-not-exist")
    assert notify_mod.notify("Titre", "Détail") is False
    assert "Titre" in caplog.text


def test_invokes_unraid_script_with_expected_arguments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    script = tmp_path / "notify"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(notify_mod, "NOTIFY_SCRIPT", script)

    assert notify_mod.notify("Bloqué", "HTTP 429", level="alert") is True
    assert "Bloqué" in calls[0]
    assert "alert" in calls[0]


def test_returns_false_and_logs_when_exec_fails(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(PermissionError("not executable")))
    script = tmp_path / "notify"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(notify_mod, "NOTIFY_SCRIPT", script)

    assert notify_mod.notify("Test", "Failed exec") is False
    assert "Test" in caplog.text
    assert "exec failed" in caplog.text
