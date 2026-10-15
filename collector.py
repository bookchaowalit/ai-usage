"""Collect quota metadata without reading credentials or conversation files.

Claude supplies documented statusLine JSON on stdin; Codex supplies documented
account/rateLimits/read through its own authenticated app-server. Only a small
allowlist is persisted. No prompts, replies, account identities or raw errors
are stored. Collection never starts an AI turn or redeems a reset credit.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_BYTES = 1024 * 1024
SCHEMA = "solo-empire.ai-usage.snapshot.v1"


class CollectionError(Exception):
    """Safe code only, never include provider output."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def state_dir() -> Path:
    configured = os.environ.get("AI_USAGE_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local/state/solo-empire/ai-usage"


def number(value: Any, maximum: float = 1e15) -> float | None:
    if type(value) not in (int, float) or not 0 <= value <= maximum:
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def window(identifier: str, label: str, value: Any, duration: int | None, *, codex: bool = False) -> dict | None:
    if not isinstance(value, dict):
        return None
    percent = number(value.get("usedPercent" if codex else "used_percentage"), 100)
    reset = number(value.get("resetsAt" if codex else "resets_at"), 253402300799)
    if percent is None or reset is None:
        return None
    return {"id": identifier, "label": label, "used_percent": percent,
            "remaining_percent": round(100 - percent, 4),
            "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat().replace("+00:00", "Z"),
            "window_minutes": duration}


def claude_snapshot(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise CollectionError("invalid_claude_statusline")
    rates = payload.get("rate_limits")
    rates = rates if isinstance(rates, dict) else {}
    windows = [w for identifier, label, duration in
               (("five_hour", "รอบ 5 ชั่วโมง", 300), ("seven_day", "รอบ 7 วัน", 10080))
               if (w := window(identifier, label, rates.get(identifier), duration)) is not None]
    cost = payload.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    # Context-window token totals are NOT lifetime/session token totals.
    return {"schema_version": SCHEMA, "provider": "claude", "source": "claude_statusline",
            "collected_at": now_iso(), "plan": None, "windows": windows,
            "session": {"estimated_cost_usd": number(cost.get("total_cost_usd")),
                        "input_tokens": None, "output_tokens": None}}


def codex_snapshot(payload: Any) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("rateLimits"), dict):
        raise CollectionError("invalid_codex_limits")
    rates = payload["rateLimits"]
    plans = {"free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite",
             "promax": "Pro Max", "team": "Team", "business": "Business", "enterprise": "Enterprise",
             "edu": "Edu", "edu_plus": "Edu", "edu_pro": "Edu", "ent26": "Enterprise",
             "self_serve_business_prolite": "Business", "self_serve_business_usage_based": "Business",
             "enterprise_cbp_automation": "Enterprise", "enterprise_cbp_usage_based": "Enterprise"}
    windows = []
    for identifier, label in (("primary", "รอบระยะสั้น"), ("secondary", "รอบระยะยาว")):
        raw = rates.get(identifier)
        duration = number(raw.get("windowDurationMins"), 525600) if isinstance(raw, dict) else None
        duration = int(duration) if duration is not None and duration > 0 and duration.is_integer() else None
        normalized = window(identifier, label, raw, duration, codex=True)
        if normalized:
            windows.append(normalized)
    return {"schema_version": SCHEMA, "provider": "codex", "source": "codex_app_server",
            "collected_at": now_iso(), "plan": plans.get(rates.get("planType")) if isinstance(rates.get("planType"), str) else None, "windows": windows,
            "session": {"estimated_cost_usd": None, "input_tokens": None, "output_tokens": None}}


def atomic_json(target: Path, payload: dict) -> None:
    parent = target.parent.absolute()
    if parent.resolve() != parent or target.is_symlink():
        raise CollectionError("unsafe_state_path")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent.chmod(0o700)
    descriptor, name = tempfile.mkstemp(prefix=".ai-usage-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(payload, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


SUBSCRIPTION_IDS = ("claude", "codex", "gemini", "grok", "muse")


def read_settings(target: Path) -> dict:
    """Keep other user settings when one section changes; never follow a symlink."""
    if target.is_symlink():
        raise CollectionError("unsafe_state_path")
    try:
        if not target.exists():
            return {}
        if target.stat().st_size > MAX_BYTES:
            raise CollectionError("settings_too_large")
        settings = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CollectionError("invalid_settings") from None
    if not isinstance(settings, dict):
        raise CollectionError("invalid_settings")
    return settings


def update_settings(target: Path, section: str, key: str | None, values: dict) -> None:
    settings = read_settings(target)
    current = settings.get(section) if isinstance(settings.get(section), dict) else {}
    if key is None:
        current.update(values)
    else:
        row = current.get(key) if isinstance(current.get(key), dict) else {}
        row.update(values)
        current[key] = row
    settings[section] = current
    atomic_json(target, settings)


def rpc_result(process: subprocess.Popen, request_id: int, deadline: float) -> dict:
    total = 0
    buffer = b""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise CollectionError("codex_connection_closed")
            total += len(chunk)
            if total > MAX_BYTES:
                raise CollectionError("codex_response_too_large")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(response, dict) and response.get("id") == request_id:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise CollectionError("codex_usage_unavailable")
                    return response["result"]
    raise CollectionError("codex_timeout")


def read_codex(binary: str | None = None, timeout: float = 20) -> dict:
    executable = binary or os.environ.get("AI_USAGE_CODEX_BIN") or shutil.which("codex")
    if not executable:
        raise CollectionError("codex_not_installed")
    deadline = time.monotonic() + timeout
    # No thread/start, turn/start, credential-file reads, or model invocations.
    try:
        process = subprocess.Popen([executable, "-c", "analytics.enabled=false", "-c", "mcp_servers={}",
                                    "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise CollectionError("codex_not_installed") from None
    try:
        def send(value: dict):
            process.stdin.write((json.dumps(value) + "\n").encode())
            process.stdin.flush()
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "solo_ai_usage", "version": "0.1.0"},
            "capabilities": {"experimentalApi": False, "explicitGatewayOauth": True}}})
        rpc_result(process, 1, deadline)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}})
        return codex_snapshot(rpc_result(process, 2, deadline))
    except (BrokenPipeError, OSError):
        raise CollectionError("codex_connection_closed") from None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()


def statusline_command(settings: Path | None) -> str | None:
    if settings is None or settings.is_symlink():
        return None
    try:
        if settings.stat().st_size > MAX_BYTES:
            return None
        value = json.loads(settings.read_text()).get("statusLine", {})
        command = value.get("command") if isinstance(value, dict) else None
        if isinstance(command, str) and len(command) <= 8192 and "collector.py" not in command:
            return command
    except (OSError, ValueError, AttributeError):
        pass
    return None


def ingest_claude(directory: Path, *, passthrough: str | None = None) -> None:
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(raw) > MAX_BYTES:
            raise CollectionError("claude_input_too_large")
        snapshot = claude_snapshot(json.loads(raw))
        # A callback without rate_limits must not refresh an old quota's timestamp.
        if snapshot["windows"]:
            atomic_json(directory / "claude.json", snapshot)
        if not passthrough:
            parts = [f"{w['label']}: เหลือ {w['remaining_percent']:g}%" for w in snapshot["windows"]]
            print("Claude · " + (" · ".join(parts) or "รอโควตาจากเซสชัน"))
    except (ValueError, UnicodeError, OSError, CollectionError):
        if not passthrough:
            print("Claude · ยังไม่มีข้อมูลโควตา")
    if passthrough:
        # This exact pre-existing command is supplied by the local installer,
        # never by an HTTP request or provider JSON.
        try:
            subprocess.run(passthrough, shell=True, input=raw, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def install_claude(settings_path: Path, fallback_path: Path | None) -> None:
    """Add the callback only, preserving every unrelated project setting."""
    if settings_path.is_symlink() or settings_path.parent.resolve() != settings_path.parent.absolute():
        raise CollectionError("unsafe_settings_path")
    try:
        original = settings_path.read_bytes() if settings_path.exists() else None
        if original and len(original) > MAX_BYTES:
            raise CollectionError("settings_too_large")
        settings = json.loads(original) if original else {}
        if not isinstance(settings, dict):
            raise CollectionError("invalid_settings")
    except (ValueError, OSError):
        raise CollectionError("invalid_settings") from None
    script = Path(__file__).resolve()
    command = shlex.join([sys.executable, str(script), "claude-statusline", "--state-dir", str(state_dir())])
    if fallback_path:
        command += " " + shlex.join(["--passthrough-settings", str(fallback_path)])
    current = settings.get("statusLine")
    if current and current != {"type": "command", "command": command}:
        raise CollectionError("existing_statusline_preserved")
    settings["statusLine"] = {"type": "command", "command": command}
    # Refuse to overwrite a concurrent edit between read and write.
    present = settings_path.read_bytes() if settings_path.exists() else None
    if present != original:
        raise CollectionError("settings_changed")
    atomic_json(settings_path, settings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("codex", "claude-statusline", "install-claude", "configure-fees",
                                                "configure-subscription", "configure-business"))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--interval", type=int, default=0, help="Codex poll interval, minimum 60 seconds")
    parser.add_argument("--passthrough", help="Preserve a pre-existing Claude statusLine command")
    parser.add_argument("--passthrough-settings", type=Path, help="Read the inherited statusLine command at callback time")
    parser.add_argument("--settings-path", type=Path, help="Claude project-local settings path")
    parser.add_argument("--claude-fee", type=float)
    parser.add_argument("--codex-fee", type=float)
    parser.add_argument("--provider", choices=SUBSCRIPTION_IDS, help="configure-subscription target")
    parser.add_argument("--fee-thb", type=float, help="Monthly subscription fee in THB")
    parser.add_argument("--plan", help="Plan label, letters/digits/space/+.- up to 40 characters")
    parser.add_argument("--active-days", type=int, help="Days used in the last 30 (self-reported)")
    parser.add_argument("--month", help="Business month YYYY-MM")
    parser.add_argument("--revenue-thb", type=float)
    parser.add_argument("--profit-before-ai-thb", type=float)
    args = parser.parse_args()
    directory = args.state_dir or state_dir()
    if args.command == "configure-fees":
        if number(args.claude_fee) is None or number(args.codex_fee) is None:
            parser.error("both monthly USD fees must be finite nonnegative numbers")
        try:
            update_settings(directory / "settings.json", "monthly_fees_usd", None,
                            {"claude": args.claude_fee, "codex": args.codex_fee})
        except CollectionError as error:
            print(str(error), file=sys.stderr)
            return 1
        print("monthly_fees_configured")
        return 0
    if args.command == "configure-subscription":
        values: dict[str, Any] = {}
        if args.provider is None:
            parser.error("--provider is required")
        if args.fee_thb is not None:
            if number(args.fee_thb, 1e12) is None:
                parser.error("--fee-thb must be a finite nonnegative number")
            values["monthly_fee_thb"] = args.fee_thb
        if args.plan is not None:
            values["plan"] = args.plan
        if args.active_days is not None:
            if not 0 <= args.active_days <= 30:
                parser.error("--active-days must be 0..30")
            values["active_days_30d"] = args.active_days
        if not values:
            parser.error("set at least one of --fee-thb, --plan, --active-days")
        try:
            update_settings(directory / "settings.json", "subscriptions", args.provider, values)
        except CollectionError as error:
            print(str(error), file=sys.stderr)
            return 1
        print("subscription_configured")
        return 0
    if args.command == "configure-business":
        values = {}
        if args.revenue_thb is not None:
            if number(args.revenue_thb, 1e12) is None:
                parser.error("--revenue-thb must be a finite nonnegative number")
            values["monthly_revenue_thb"] = args.revenue_thb
        if args.profit_before_ai_thb is not None:
            if not math.isfinite(args.profit_before_ai_thb) or abs(args.profit_before_ai_thb) > 1e12:
                parser.error("--profit-before-ai-thb must be a finite number")
            values["monthly_profit_before_ai_thb"] = args.profit_before_ai_thb
        if args.month is not None:
            values["month"] = args.month
        if not values:
            parser.error("set at least one of --revenue-thb, --profit-before-ai-thb, --month")
        try:
            update_settings(directory / "settings.json", "business", None, values)
        except CollectionError as error:
            print(str(error), file=sys.stderr)
            return 1
        print("business_configured")
        return 0
    if args.command == "install-claude":
        if args.settings_path is None:
            parser.error("--settings-path is required")
        try:
            install_claude(args.settings_path, args.passthrough_settings)
        except CollectionError as error:
            print(str(error), file=sys.stderr)
            return 1
        print("claude_statusline_configured")
        return 0
    if args.command == "claude-statusline":
        ingest_claude(directory, passthrough=args.passthrough or statusline_command(args.passthrough_settings))
        return 0
    if args.interval and args.interval < 60:
        parser.error("poll interval must be at least 60 seconds")
    while True:
        try:
            snapshot = read_codex()
            atomic_json(directory / "codex.json", snapshot)
            print(json.dumps({"provider": "codex", "status": "collected", "windows": len(snapshot["windows"])}), flush=True)
            success = True
        except (CollectionError, ValueError, OSError):
            # Retain last success; its original timestamp naturally becomes stale.
            print(json.dumps({"provider": "codex", "status": "unavailable"}), flush=True)
            success = False
        if not args.interval:
            return 0 if success else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
