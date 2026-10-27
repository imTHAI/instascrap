import logging
from datetime import datetime, timezone

from instascrap.alerts import Alerter, alert_blocked


def test_alerter_posts_colored_embed():
    calls = []
    Alerter("https://discord.test/hook", post=lambda url, body: calls.append((url, body))).send("alert", "T", "M")
    assert calls == [("https://discord.test/hook",
                      {"embeds": [{"title": "T", "description": "M", "color": 0xE74C3C}]})]


def test_alerter_without_webhook_only_logs(caplog):
    caplog.set_level(logging.INFO)
    Alerter(None).send("warning", "T", "M")
    assert "T" in caplog.text and "M" in caplog.text


def test_alerter_swallows_post_failure(caplog):
    def boom(url, body):
        raise OSError("down")
    Alerter("https://discord.test/hook", post=boom).send("info", "T", "M")
    assert "down" in caplog.text


def test_block_alert_names_resume_time_in_local_zone():
    sent = []
    alerter = Alerter(None)
    alerter.send = lambda level, title, message: sent.append((level, title, message))
    alert_blocked(alerter, "HTTP 429 on x", datetime(2026, 10, 28, 7, 0, tzinfo=timezone.utc), "Europe/Paris")
    assert sent == [("alert", "Instagram is throttling",
                     "HTTP 429 on x. Pausing until 2026-10-28 08:00 CET.")]
