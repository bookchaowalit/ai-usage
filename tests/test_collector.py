import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("collector", Path(__file__).parents[1] / "collector.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def test_claude_keeps_only_documented_quota_and_session_estimate():
    result = collector.claude_snapshot({"rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1900000000},
        "seven_day": {"used_percentage": 41.2, "resets_at": 1900500000}},
        "cost": {"total_cost_usd": 12.3}, "session_id": "private-id", "cwd": "/private/path",
        "context_window": {"total_input_tokens": 1000}, "prompt": "private text"})
    assert result["windows"][0]["remaining_percent"] == 76.5
    assert result["session"]["estimated_cost_usd"] == 12.3
    assert result["session"]["input_tokens"] is None
    assert not any(value in json.dumps(result) for value in ("private-id", "/private/path", "private text"))


@pytest.mark.parametrize("invalid", [None, True, -1, 101, float("nan"), float("inf"), "25"])
def test_invalid_quota_never_becomes_zero(invalid):
    assert collector.claude_snapshot({"rate_limits": {"five_hour": {"used_percentage": invalid, "resets_at": 1900000000}}})["windows"] == []


def test_codex_uses_main_bucket_not_model_specific_assumptions():
    result = collector.codex_snapshot({"rateLimits": {"planType": "prolite", "primary": {
        "usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1900000000}, "secondary": None},
        "rateLimitsByLimitId": {"private-model": {"limitName": "private-account"}}, "token": "private-key"})
    assert result["plan"] == "Pro Lite"
    assert result["windows"][0]["window_minutes"] == 300
    assert result["windows"][0]["remaining_percent"] == 75
    assert "private-" not in json.dumps(result)


def test_unknown_duration_not_invented():
    result = collector.codex_snapshot({"rateLimits": {"primary": {"usedPercent": 5, "resetsAt": 1900000000}}})
    assert result["windows"][0]["window_minutes"] is None
    assert result["plan"] is None


def test_atomic_metadata_is_private_and_symlinks_rejected(tmp_path):
    path = tmp_path / "state" / "codex.json"
    collector.atomic_json(path, {"provider": "codex"})
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    target = tmp_path / "target.json"
    target.write_text("keep")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(collector.CollectionError):
        collector.atomic_json(path, {})
    assert target.read_text() == "keep"


def test_statusline_install_preserves_other_settings_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_USAGE_STATE_DIR", str(tmp_path / "state"))
    settings = tmp_path / "claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": []}}))
    collector.install_claude(settings, tmp_path / "inherited.json")
    collector.install_claude(settings, tmp_path / "inherited.json")
    current = json.loads(settings.read_text())
    assert current["permissions"] == {"allow": ["Read"]}
    assert current["hooks"] == {"Stop": []}
    assert "--passthrough-settings" in current["statusLine"]["command"]


def test_different_local_statusline_is_never_overwritten(tmp_path):
    settings = tmp_path / "settings.local.json"
    original = {"statusLine": {"type": "command", "command": "existing-script"}}
    settings.write_text(json.dumps(original))
    with pytest.raises(collector.CollectionError, match="existing_statusline_preserved"):
        collector.install_claude(settings, None)
    assert json.loads(settings.read_text()) == original


def test_statusline_passthrough_does_not_recurse_or_publish_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"command": "printf ready"}, "private": "never return"}))
    assert collector.statusline_command(settings) == "printf ready"
    settings.write_text(json.dumps({"statusLine": {"command": "python collector.py claude-statusline"}}))
    assert collector.statusline_command(settings) is None


def test_symlink_ancestor_is_rejected_before_creation(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(collector.CollectionError, match="unsafe_state_path"):
        collector.atomic_json(link / "child" / "codex.json", {})
    assert not (target / "child").exists()


def test_malformed_plan_and_huge_numeric_values_remain_unknown():
    result = collector.codex_snapshot({"rateLimits": {"planType": {"secret": "do not publish"}}})
    assert result["plan"] is None
    assert collector.number(10**1000) is None


def fake_codex(tmp_path, body):
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return str(executable)


def test_codex_transport_only_initializes_and_reads_account_metadata(tmp_path):
    binary = fake_codex(tmp_path, '''import json,sys
expected = ["initialize", "initialized", "account/rateLimits/read"]
for line in sys.stdin:
    request=json.loads(line)
    assert request["method"]==expected.pop(0)
    if request["method"]=="initialize":
        assert request["params"]["clientInfo"]["name"]=="solo_ai_usage"
        print(json.dumps({"id":request["id"],"result":{}}),flush=True)
    elif request["method"]=="account/rateLimits/read":
        assert request["params"]=={"excludeResetCreditDetails":True}
        print(json.dumps({"method":"irrelevant/notification","params":{"secret":"never store"}}),flush=True)
        print(json.dumps({"id":request["id"],"result":{"rateLimits":{"primary":{"usedPercent":25,"windowDurationMins":300,"resetsAt":1900000000}}}}),flush=True)
''')
    result = collector.read_codex(binary, timeout=3)
    assert result["windows"][0]["remaining_percent"] == 75
    assert "never store" not in json.dumps(result)


def test_codex_transport_timeout_terminates_its_process(tmp_path):
    pid = tmp_path / "process.pid"
    binary = fake_codex(tmp_path, f'''import os,sys
open({str(pid)!r},"w").write(str(os.getpid()))
for line in sys.stdin:
    pass
''')
    with pytest.raises(collector.CollectionError, match="codex_timeout"):
        collector.read_codex(binary, timeout=0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_codex_transport_does_not_return_provider_error_payload(tmp_path):
    binary = fake_codex(tmp_path, '''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({"id":request["id"],"error":{"message":"sensitive-example"}}),flush=True)
for line in sys.stdin:
    pass
''')
    with pytest.raises(collector.CollectionError) as error:
        collector.read_codex(binary, timeout=3)
    assert str(error.value) == "codex_usage_unavailable"


def test_configure_commands_merge_settings_sections(tmp_path, monkeypatch):
    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["collector.py", *argv, "--state-dir", str(tmp_path)])
        return collector.main()

    assert run("configure-business", "--revenue-thb", "80000", "--profit-before-ai-thb", "-500", "--month", "2026-10") == 0
    assert run("configure-subscription", "--provider", "gemini", "--fee-thb", "700", "--plan", "AI Pro") == 0
    assert run("configure-subscription", "--provider", "gemini", "--active-days", "3") == 0
    assert run("configure-fees", "--claude-fee", "100", "--codex-fee", "20") == 0
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings == {
        "business": {"monthly_revenue_thb": 80000, "monthly_profit_before_ai_thb": -500, "month": "2026-10"},
        "subscriptions": {"gemini": {"monthly_fee_thb": 700, "plan": "AI Pro", "active_days_30d": 3}},
        "monthly_fees_usd": {"claude": 100, "codex": 20},
    }
    with pytest.raises(SystemExit):
        run("configure-subscription", "--provider", "grok", "--fee-thb", "-1")
    (tmp_path / "settings.json").write_text("[]")
    assert run("configure-subscription", "--provider", "grok", "--fee-thb", "1") == 1


def test_status_renders_fresh_stale_and_missing_providers():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    text = collector.render_status({
        "claude": {"collected_at": "2026-10-17T11:58:00Z", "plan": None, "windows": [
            {"remaining_percent": 62, "resets_at": "2026-10-17T14:14:00Z", "window_minutes": 300},
            {"remaining_percent": 80, "resets_at": "2026-10-17T11:00:00Z", "window_minutes": 10080}]},
        "codex": None}, now)
    assert "5-hour   ██████░░░░   62% left · resets in 2h 14m" in text
    assert "weekly   reset since last check" in text
    assert "Codex   not connected" in text
    assert "stale" not in text and "\033[" not in text
    stale = collector.render_status({"codex": {"collected_at": "2026-10-17T09:00:00Z", "plan": "Plus", "windows": [
        {"remaining_percent": 35, "resets_at": "2026-10-22T12:00:00Z", "window_minutes": 10080}]}}, now)
    assert "Codex · Plus (stale, 3h 00m ago)" in stale
    assert "resets in 5d 0h" in stale


def test_status_command_reads_stored_snapshots_offline(tmp_path, monkeypatch, capsys):
    (tmp_path / "codex.json").write_text(json.dumps({"collected_at": "2000-01-01T00:00:00Z", "windows": [
        {"remaining_percent": 10, "resets_at": "2999-01-01T00:00:00Z", "window_minutes": 300}]}))
    monkeypatch.setattr(sys, "argv", ["collector.py", "status", "--offline", "--state-dir", str(tmp_path)])
    assert collector.main() == 0
    out = capsys.readouterr().out
    assert "Claude  not connected" in out and "10% left" in out and "stale" in out
