import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.notify import (Notifier, build_message, plan_alerts, row_key,
                        validate_public_url, validate_webhook_url)

HOOK = "https://discord.com/api/webhooks/123456/abcdefTOKEN1234"


def test_webhook_validation_accepts_discord():
    assert validate_webhook_url(f"  {HOOK} ") == HOOK
    assert validate_webhook_url("https://ptb.discord.com/api/webhooks/1/x")


@pytest.mark.parametrize("bad", [
    "http://discord.com/api/webhooks/1/x",              # not https
    "https://example.com/api/webhooks/1/x",             # wrong host
    "https://discord.com.evil.com/api/webhooks/1/x",    # look-alike host
    "https://discord.com@evil.com/api/webhooks/1/x",    # userinfo trick
    "https://discord.com/other/path",                   # wrong path
    "https://discord.com/api/webhooks/",                # no id/token
    "not a url", "",
])
def test_webhook_validation_rejects(bad):
    with pytest.raises(ValueError):
        validate_webhook_url(bad)


def test_public_url_validation():
    assert validate_public_url("http://10.0.0.49:8080") == "http://10.0.0.49:8080"
    with pytest.raises(ValueError):
        validate_public_url("10.0.0.49:8080")


def make_notifier(tmp_path, monkeypatch, env_hook=""):
    monkeypatch.setenv("SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "alerted.json"))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", env_hook)
    monkeypatch.setenv("PUBLIC_URL", "")
    return Notifier()


def test_settings_round_trip_and_hide_url(tmp_path, monkeypatch):
    n = make_notifier(tmp_path, monkeypatch)
    assert not n.enabled
    n.update_settings({"discord_webhook_url": HOOK, "public_url": "http://h:8080"})
    assert n.enabled and n.source == "settings"
    view = n.public_view()
    assert HOOK not in str(view) and view["webhook_hint"].endswith("1234")
    # survives a restart
    again = make_notifier(tmp_path, monkeypatch)
    assert again.enabled and again.public_url == "http://h:8080"


def test_omitted_field_is_kept_and_empty_clears(tmp_path, monkeypatch):
    n = make_notifier(tmp_path, monkeypatch)
    n.update_settings({"discord_webhook_url": HOOK})
    n.update_settings({"public_url": "http://h:8080"})   # webhook omitted -> unchanged
    assert n.enabled
    n.update_settings({"discord_webhook_url": ""})       # explicit clear
    assert not n.enabled and n.public_url == "http://h:8080"


def test_saved_settings_override_env_and_clear_disables(tmp_path, monkeypatch):
    n = make_notifier(tmp_path, monkeypatch, env_hook=HOOK)
    assert n.enabled and n.source == "env"
    n.update_settings({"discord_webhook_url": ""})
    assert not n.enabled
    assert not make_notifier(tmp_path, monkeypatch, env_hook=HOOK).enabled


def test_invalid_input_not_saved(tmp_path, monkeypatch):
    n = make_notifier(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        n.update_settings({"discord_webhook_url": "https://evil.example/x"})
    assert not n.enabled and not (tmp_path / "settings.json").exists()


def row(machine, name, status, image="img:latest"):
    return {"machine": machine, "container": name, "status": status, "image": image}


def test_alerts_only_new_outdated():
    rows = [row("a", "x", "update_available"), row("a", "y", "up_to_date")]
    pending, keep = plan_alerts(rows, set())
    assert [r["container"] for r in pending] == ["x"] and keep == set()


def test_no_repeat_alert_while_still_outdated():
    rows = [row("a", "x", "update_available")]
    pending, keep = plan_alerts(rows, {row_key(rows[0])})
    assert pending == [] and keep == {row_key(rows[0])}


def test_key_dropped_after_update_so_next_update_alerts_again():
    k = "a|x"
    _, keep = plan_alerts([row("a", "x", "up_to_date")], {k})
    assert keep == set()
    pending, _ = plan_alerts([row("a", "x", "update_available")], keep)
    assert len(pending) == 1


def test_unknown_status_keeps_key_to_avoid_repeat_alert():
    _, keep = plan_alerts([row("a", "x", "unknown")], {"a|x"})
    assert keep == {"a|x"}
    pending, _ = plan_alerts([row("a", "x", "update_available")], keep)
    assert pending == []


def test_removed_container_forgotten():
    _, keep = plan_alerts([], {"a|gone"})
    assert keep == set()


def test_same_name_on_different_machines_are_separate():
    rows = [row("a", "x", "update_available"), row("b", "x", "update_available")]
    pending, _ = plan_alerts(rows, {"a|x"})
    assert [r["machine"] for r in pending] == ["b"]


def test_message_groups_by_machine_and_pluralises():
    msg = build_message([row("m1", "x", "update_available"), row("m1", "y", "update_available"),
                         row("m2", "z", "update_available")], "http://h:8080")
    e = msg["embeds"][0]
    assert e["title"] == "3 container updates available"
    assert e["url"] == "http://h:8080"
    assert e["description"].count("**m1**") == 1 and "**m2**" in e["description"]
    assert build_message([row("m", "x", "update_available")])["embeds"][0]["title"] == "1 container update available"


def test_message_truncates_long_lists():
    pending = [row("m", f"c{i}", "update_available") for i in range(100)]
    e = build_message(pending, limit=40)["embeds"][0]
    assert "…and 60 more" in e["description"] and len(e["description"]) <= 4000
