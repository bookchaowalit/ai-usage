"""Bounded, non-secret projection of local AI usage snapshots and subscription ROI."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "solo-empire.ai-usage.v1"
SNAPSHOT_SCHEMA_VERSION = "solo-empire.ai-usage.snapshot.v1"
MAX_SNAPSHOT_BYTES = 1024 * 1024
STALE_SECONDS = 900
FUTURE_TOLERANCE_SECONDS = 60
PROVIDERS = {
    "claude": ("Claude", "claude_statusline"),
    "codex": ("Codex", "codex_app_server"),
}
PLANS = {
    "claude": ("Free", "Pro", "Max", "Team", "Enterprise"),
    "codex": (
        "Free",
        "Go",
        "Plus",
        "Pro",
        "Pro Lite",
        "Pro Max",
        "Team",
        "Business",
        "Enterprise",
        "Edu",
    ),
}
WINDOW_LABELS = {
    "five_hour": "รอบ 5 ชั่วโมง",
    "seven_day": "รอบ 7 วัน",
    "primary": "รอบหลัก",
    "secondary": "รอบรอง",
}
PROVIDER_WINDOWS = {
    "claude": {"five_hour", "seven_day"},
    "codex": {"primary", "secondary"},
}
# Every paid AI; only PROVIDERS have a quota collector, the rest are cost-only.
SUBSCRIPTIONS = {
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "grok": "Grok",
    "muse": "Muse",
}
PLAN_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 +.\-]{0,39}")
MONTH_TEXT = re.compile(r"20[0-9]{2}-(0[1-9]|1[0-2])")
MAX_AMOUNT_THB = 1e12
TARGET_PROFIT_RATIO = 3
BUDGET_BAND_PERCENT = (5, 10)
NEAR_ZERO_USED_PERCENT = 5
NEAR_ZERO_ACTIVE_DAYS = 2


def utc_iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162 - keeps Z-normalisation; removing it changes which inputs are accepted
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def nonnegative_number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except (ValueError, OverflowError):
        return None


def token_count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def monthly_fee(
    environ: Mapping[str, str], provider: str, settings: Mapping[str, Any]
) -> float | None:
    key = f"AI_USAGE_{provider.upper()}_MONTHLY_FEE_USD"
    if key not in environ:
        fees = settings.get("monthly_fees_usd")
        return (
            nonnegative_number(fees.get(provider)) if isinstance(fees, dict) else None
        )
    raw = environ[key]
    try:
        return nonnegative_number(float(raw)) if raw is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def thb_amount(value: Any, *, signed: bool = False) -> float | None:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or abs(value) > MAX_AMOUNT_THB
    ):
        return None
    return float(value) if signed or value >= 0 else None


def env_or_setting(
    environ: Mapping[str, str], key: str, setting: Any, *, signed: bool = False
) -> float | None:
    """Environment overrides settings, even when the override is invalid (then unknown)."""
    if key not in environ:
        return thb_amount(setting, signed=signed)
    try:
        return thb_amount(float(environ[key]), signed=signed)
    except (ValueError, TypeError, OverflowError):
        return None


def default_state_dir() -> Path:
    configured = os.environ.get("AI_USAGE_STATE_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state" / "solo-empire" / "ai-usage"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate snapshot field")
        result[key] = value
    return result


def _open_directory(state_dir: Path) -> int:
    """Reject symlinks in every path component, including ancestor directories."""
    absolute = Path(os.path.abspath(state_dir))
    directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def read_snapshot(state_dir: Path, provider: str) -> dict[str, Any] | None:
    """Open only allowlisted regular files without following symlinks or FIFOs."""
    if provider not in {*PROVIDERS, "settings"}:
        raise ValueError("invalid snapshot name")
    directory = _open_directory(state_dir)
    try:
        try:
            fd = os.open(
                provider + ".json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as snapshot:
            metadata = os.fstat(snapshot.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > MAX_SNAPSHOT_BYTES
            ):
                raise ValueError("invalid snapshot file")
            encoded = snapshot.read(MAX_SNAPSHOT_BYTES + 1)
            if len(encoded) > MAX_SNAPSHOT_BYTES:
                raise ValueError("snapshot too large")
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict):
            raise ValueError("invalid snapshot object")  # noqa: TRY004 - read_snapshot reports every malformed snapshot as ValueError
        return raw
    finally:
        os.close(directory)


def project_window(raw: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    used = nonnegative_number(raw.get("used_percent"))
    used = min(100.0, used) if used is not None else None
    reset = parse_timestamp(raw.get("resets_at"))
    expired = reset is not None and reset <= now
    minutes = raw.get("window_minutes")
    minutes = minutes if type(minutes) is int and 0 < minutes <= 525600 else None
    label = WINDOW_LABELS[raw["id"]]
    if raw["id"] in {"primary", "secondary"} and minutes is not None:
        if minutes % 1440 == 0:
            label = f"รอบ {minutes // 1440} วัน"
        elif minutes % 60 == 0:
            label = f"รอบ {minutes // 60} ชั่วโมง"
        else:
            label = f"รอบ {minutes} นาที"
    return {
        "id": raw["id"],
        "label": label,
        "used_percent": used,
        "remaining_percent": round(100 - used, 6)
        if used is not None and not expired
        else None,
        "resets_at": utc_iso(reset) if reset else None,
        "window_minutes": minutes,
        "status": "expired" if expired else "active",
    }


def window_alerts(
    provider: str, window: Mapping[str, Any], now: datetime
) -> list[dict[str, Any]]:
    used, remaining = window["used_percent"], window["remaining_percent"]
    reset = parse_timestamp(window["resets_at"])
    if window["status"] != "active" or used is None or reset is None:
        return []
    kind = None
    if used >= 100:
        kind = "quota_reached"
    elif used >= 85:
        kind = "quota_low"
    else:
        minutes = window["window_minutes"]
        warning_seconds = (
            6 * 3600
            if minutes is not None and minutes <= 360
            else (24 * 3600 if minutes is not None and minutes >= 10080 else None)
        )
        if (
            remaining is not None
            and remaining >= 30
            and warning_seconds is not None
            and 0 < (reset - now).total_seconds() <= warning_seconds
        ):
            kind = "use_before_reset"
    if kind is None:
        return []
    name = PROVIDERS[provider][0]
    messages = {
        "quota_reached": f"{name} ใช้โควตา{window['label']}ครบแล้ว รอรอบรีเซ็ต",
        "quota_low": f"{name} เหลือโควตา{window['label']} {remaining:g}%",
        "use_before_reset": f"{name} เหลือโควตา{window['label']} {remaining:g}% ใกล้รีเซ็ต ใช้กับงานที่ค้างได้",
    }
    return [
        {
            "id": f"{provider}:{window['id']}:{kind}:{window['resets_at']}",
            "kind": kind,
            "window_id": window["id"],
            "message": messages[kind],
            "resets_at": window["resets_at"],
            "remaining_percent": remaining,
        }
    ]


def empty_provider(
    provider: str, fee: float | None, status: str = "not_connected"
) -> dict[str, Any]:
    return {
        "id": provider,
        "name": PROVIDERS[provider][0],
        "status": status,
        "source": "not_connected",
        "collected_at": None,
        "plan": None,
        "monthly_fee_usd": fee,
        "windows": [],
        "session": {
            "estimated_cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
        },
        "api_billed_cost_usd": None,
        "alerts": [],
        "next_action": "connect_provider"
        if status == "not_connected"
        else "check_source",
    }


def project_provider(
    provider: str, raw: Mapping[str, Any], now: datetime, fee: float | None
) -> dict[str, Any]:
    collected = parse_timestamp(raw.get("collected_at"))
    if (
        raw.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or raw.get("provider") != provider
        or raw.get("source") != PROVIDERS[provider][1]
        or collected is None
        or (collected - now).total_seconds() > FUTURE_TOLERANCE_SECONDS
    ):
        return empty_provider(provider, fee, "error")
    result = empty_provider(provider, fee)
    result.update(
        status="stale"
        if (now - collected).total_seconds() > STALE_SECONDS
        else "ready",
        source=PROVIDERS[provider][1],
        collected_at=utc_iso(collected),
    )
    plan = raw.get("plan")
    if isinstance(plan, str):
        result["plan"] = next(
            (label for label in PLANS[provider] if plan.casefold() == label.casefold()),
            None,
        )
    session = raw.get("session")
    if isinstance(session, dict):
        result["session"] = {
            "estimated_cost_usd": nonnegative_number(session.get("estimated_cost_usd")),
            "input_tokens": token_count(session.get("input_tokens")),
            "output_tokens": token_count(session.get("output_tokens")),
        }
    windows = raw.get("windows")
    if isinstance(windows, list) and len(windows) <= 20:
        allowed = [
            row
            for row in windows
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row["id"] in PROVIDER_WINDOWS[provider]
        ]
        counts = Counter(row["id"] for row in allowed)
        result["windows"] = [
            project_window(row, now) for row in allowed if counts[row["id"]] == 1
        ]
    if not any(row["used_percent"] is not None for row in result["windows"]):
        result["status"] = "error"
    if result["status"] == "ready":
        result["alerts"] = [
            alert
            for row in result["windows"]
            for alert in window_alerts(provider, row, now)
        ]
        quota_blocked = any(
            row["status"] == "active" and row["remaining_percent"] == 0
            for row in result["windows"]
        )
        if quota_blocked:
            result["alerts"] = [
                alert
                for alert in result["alerts"]
                if alert["kind"] != "use_before_reset"
            ]
        kinds = {alert["kind"] for alert in result["alerts"]}
        result["next_action"] = next(
            (
                kind
                for kind in ("quota_reached", "quota_low", "use_before_reset")
                if kind in kinds
            ),
            "monitor",
        )
    else:
        result["next_action"] = (
            "refresh_usage" if result["status"] == "stale" else "check_source"
        )
    return result


def quota_usage(
    row: Mapping[str, Any] | None, now: datetime
) -> tuple[str, float | None]:
    """Near zero only when a fresh weekly-or-longer window is at least half elapsed."""
    if row is None or row["status"] != "ready":
        return "unknown", None
    windows = [
        w
        for w in row["windows"]
        if w["status"] == "active" and w["used_percent"] is not None
    ]
    longest = max(
        windows,
        key=lambda w: w["window_minutes"] or (10080 if w["id"] == "seven_day" else 0),
        default=None,
    )
    if longest is None:
        return "unknown", None
    used = longest["used_percent"]
    minutes = longest["window_minutes"] or (
        10080 if longest["id"] == "seven_day" else None
    )
    reset = parse_timestamp(longest["resets_at"])
    if used >= NEAR_ZERO_USED_PERCENT:
        return "active", used
    if minutes is None or minutes < 10080 or reset is None:
        return "unknown", used
    elapsed = minutes * 60 - (reset - now).total_seconds()
    return ("near_zero" if elapsed >= minutes * 30 else "unknown"), used


def project_subscription(
    provider: str,
    raw: Any,
    environ: Mapping[str, str],
    quota_row: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    plan = raw.get("plan")
    plan = plan if isinstance(plan, str) and PLAN_TEXT.fullmatch(plan) else None
    if quota_row is not None and quota_row["plan"] is not None:
        plan = quota_row["plan"]
    fee = env_or_setting(
        environ,
        f"AI_USAGE_{provider.upper()}_MONTHLY_FEE_THB",
        raw.get("monthly_fee_thb"),
    )
    days = raw.get("active_days_30d")
    days = days if type(days) is int and 0 <= days <= 30 else None
    level, used = quota_usage(quota_row, now)
    signal = "quota" if level != "unknown" else "unknown"
    if signal == "unknown" and days is not None:
        signal, level = (
            "self_reported",
            ("near_zero" if days <= NEAR_ZERO_ACTIVE_DAYS else "active"),
        )
    pause = fee is not None and fee > 0 and level == "near_zero"
    return {
        "id": provider,
        "name": SUBSCRIPTIONS[provider],
        "plan": plan,
        "monthly_fee_thb": fee,
        "has_quota_collector": provider in PROVIDERS,
        "usage": {
            "signal": signal,
            "level": level,
            "used_percent": used if signal == "quota" else None,
            "active_days_30d": days,
        },
        "consider_pausing": pause,
    }


def project_roi(
    subscriptions: list[dict[str, Any]], business: Any, environ: Mapping[str, str]
) -> dict[str, Any]:
    business = business if isinstance(business, dict) else {}
    month = business.get("month")
    month = month if isinstance(month, str) and MONTH_TEXT.fullmatch(month) else None
    revenue = env_or_setting(
        environ, "AI_USAGE_MONTHLY_REVENUE_THB", business.get("monthly_revenue_thb")
    )
    profit = env_or_setting(
        environ,
        "AI_USAGE_MONTHLY_PROFIT_BEFORE_AI_THB",
        business.get("monthly_profit_before_ai_thb"),
        signed=True,
    )
    missing = [row["id"] for row in subscriptions if row["monthly_fee_thb"] is None]
    spend = None if missing else sum(row["monthly_fee_thb"] for row in subscriptions)
    ratio = round(profit / spend, 4) if spend and profit is not None else None
    ratio_status = (
        "unknown"
        if ratio is None
        else ("meets_target" if ratio >= TARGET_PROFIT_RATIO else "below_target")
    )
    percent = round(spend / revenue * 100, 4) if spend is not None and revenue else None
    low, high = BUDGET_BAND_PERCENT
    budget_status = (
        "unknown"
        if percent is None
        else (
            "under_band"
            if percent < low
            else "over_band"
            if percent > high
            else "within_band"
        )
    )
    pausing = [row["id"] for row in subscriptions if row["consider_pausing"]]
    if missing:
        verdict, message = (
            "incomplete",
            "ยังไม่ครบ: ใส่ค่าสมาชิกรายเดือน (บาท) ของ "
            + ", ".join(SUBSCRIPTIONS[item] for item in missing),
        )
    elif profit is None:
        verdict, message = "incomplete", "ใส่กำไรธุรกิจรายเดือนก่อนหักค่า AI เพื่อคำนวณความคุ้มค่า"
    elif spend == 0:
        verdict, message = "no_spend", "ยังไม่มีค่าใช้จ่าย AI รายเดือน"
    elif ratio_status == "meets_target":
        verdict, message = (
            "paying_for_itself",
            f"กำไร {ratio:g} เท่าของค่า AI ผ่านเป้า {TARGET_PROFIT_RATIO} เท่า",
        )
    else:
        verdict, message = (
            "not_paying_for_itself",
            (
                f"กำไรเพียง {ratio:g} เท่าของค่า AI ต่ำกว่าเป้า {TARGET_PROFIT_RATIO} เท่า พิจารณาลดแพ็กเกจที่ใช้น้อย"
            ),
        )
    return {
        "currency": "THB",
        "month": month,
        "ai_spend_thb": spend,
        "spend_complete": not missing,
        "missing_fees": missing,
        "revenue_thb": revenue,
        "profit_before_ai_thb": profit,
        "profit_to_ai_ratio": ratio,
        "target_ratio": TARGET_PROFIT_RATIO,
        "ratio_status": ratio_status,
        "max_ai_spend_by_ratio_thb": round(profit / TARGET_PROFIT_RATIO, 2)
        if profit is not None and profit > 0
        else (0.0 if profit is not None else None),
        "ai_spend_percent_of_revenue": percent,
        "budget_band_percent": {"min": low, "max": high},
        "budget_band_thb": (
            {
                "min": round(revenue * low / 100, 2),
                "max": round(revenue * high / 100, 2),
            }
            if revenue is not None
            else None
        ),
        "budget_status": budget_status,
        "consider_pausing": pausing,
        "verdict": verdict,
        "message": message,
    }


class UsageStore:
    """Read sanitized local snapshots only; no provider APIs or canonical writes."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.state_dir = (
            Path(state_dir) if state_dir is not None else default_state_dir()
        )
        self.environ = os.environ if environ is None else environ

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        try:
            settings = read_snapshot(self.state_dir, "settings") or {}
        except (OSError, ValueError, TypeError, RecursionError):
            settings = {}
        providers = []
        for provider in PROVIDERS:
            fee = monthly_fee(self.environ, provider, settings)
            try:
                raw = read_snapshot(self.state_dir, provider)
                providers.append(
                    project_provider(provider, raw, now, fee)
                    if raw is not None
                    else empty_provider(provider, fee)
                )
            except FileNotFoundError:
                providers.append(empty_provider(provider, fee))
            except (OSError, ValueError, TypeError, RecursionError):
                providers.append(empty_provider(provider, fee, "error"))
        quota_rows = {row["id"]: row for row in providers}
        configured = settings.get("subscriptions")
        configured = configured if isinstance(configured, dict) else {}
        subscriptions = [
            project_subscription(
                provider,
                configured.get(provider),
                self.environ,
                quota_rows.get(provider),
                now,
            )
            for provider in SUBSCRIPTIONS
        ]
        fees_complete = all(row["monthly_fee_usd"] is not None for row in providers)
        total_fee = (
            sum(row["monthly_fee_usd"] for row in providers) if fees_complete else None
        )
        if total_fee is not None and not math.isfinite(total_fee):
            total_fee, fees_complete = None, False
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_iso(now),
            "providers": providers,
            "summary": {
                "connected": sum(
                    row["status"] in {"ready", "stale"} for row in providers
                ),
                "monthly_fee_usd": total_fee,
                "fees_complete": fees_complete,
            },
            "subscriptions": subscriptions,
            "roi": project_roi(subscriptions, settings.get("business"), self.environ),
        }
