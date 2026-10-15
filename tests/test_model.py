"""Offline synthetic tests for quota, reset, fees and snapshot privacy."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import MAX_SNAPSHOT_BYTES, SNAPSHOT_SCHEMA_VERSION, UsageStore, utc_iso  # noqa: E402

NOW = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)


def snapshot(provider="claude", used=40, reset_hours=2, minutes=300):
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION, "provider": provider,
        "source": "claude_statusline" if provider == "claude" else "codex_app_server",
        "collected_at": utc_iso(NOW), "plan": "Pro" if provider == "claude" else "Plus",
        "windows": [{"id": "five_hour" if provider == "claude" else "primary", "label": "PRIVATE_MARKER",
                     "used_percent": used, "remaining_percent": 999,
                     "resets_at": utc_iso(NOW + timedelta(hours=reset_hours)), "window_minutes": minutes}],
        "session": {"estimated_cost_usd": 2.5, "input_tokens": 2000, "output_tokens": 500},
    }


class UsageModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = UsageStore(self.path, environ={})

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, raw, provider="claude"):
        (self.path / (provider + ".json")).write_text(json.dumps(raw), encoding="utf-8")

    def provider(self, provider="claude"):
        return next(row for row in self.store.snapshot(now=NOW)["providers"] if row["id"] == provider)

    def test_absent_providers_are_unknown_and_fees_incomplete(self):
        result = self.store.snapshot(now=NOW)
        self.assertEqual([row["id"] for row in result["providers"]], ["claude", "codex"])
        self.assertEqual(result["summary"], {"connected": 0, "monthly_fee_usd": None, "fees_complete": False})
        for row in result["providers"]:
            self.assertEqual(row["status"], "not_connected")
            self.assertEqual(row["windows"], [])
            self.assertIsNone(row["api_billed_cost_usd"])
        missing = UsageStore(self.path / "missing", environ={}).snapshot(now=NOW)
        self.assertEqual(missing["providers"][0]["status"], "not_connected")

    def test_fresh_provider_is_allowlisted_and_api_billing_is_unknown(self):
        raw = snapshot()
        raw.update(email="PRIVATE_MARKER", token="PRIVATE_MARKER", path="PRIVATE_MARKER", api_billed_cost_usd=99)
        raw["session"]["raw_error"] = "PRIVATE_MARKER"
        self.save(raw)
        result = self.provider()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["windows"][0]["remaining_percent"], 60)
        self.assertEqual(result["session"]["estimated_cost_usd"], 2.5)
        self.assertIsNone(result["api_billed_cost_usd"])
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))
        self.assertEqual(result["alerts"][0]["kind"], "use_before_reset")

    def test_alert_thresholds_and_actual_duration(self):
        for used, hours, minutes, expected in (
            (84, 7, 300, None), (85, 7, 300, "quota_low"),
            (99.9, 7, 300, "quota_low"), (100, 7, 300, "quota_reached"),
            (120, 7, 300, "quota_reached"), (70, 6, 300, "use_before_reset"),
            (71, 2, 300, None), (40, 7, 300, None),
            (40, 24, 10080, "use_before_reset"), (40, 25, 10080, None),
            (40, 2, 1440, None), (40, 2, None, None),
        ):
            with self.subTest(used=used, hours=hours, minutes=minutes):
                self.save(snapshot(used=used, reset_hours=hours, minutes=minutes))
                alerts = self.provider()["alerts"]
                self.assertEqual(alerts[0]["kind"] if alerts else None, expected)

    def test_alert_id_changes_for_reset_cycle_only(self):
        self.save(snapshot())
        initial = self.provider()["alerts"][0]["id"]
        later = self.store.snapshot(now=NOW + timedelta(minutes=1))["providers"][0]["alerts"][0]["id"]
        self.assertEqual(initial, later)
        self.save(snapshot(reset_hours=3))
        self.assertNotEqual(initial, self.provider()["alerts"][0]["id"])

    def test_exhausted_other_window_blocks_use_before_reset_for_each_provider(self):
        for provider, other_id in (("claude", "seven_day"), ("codex", "secondary")):
            for duration in (10080, None):
                with self.subTest(provider=provider, duration=duration):
                    raw = snapshot(provider, used=25, reset_hours=1)
                    raw["windows"].append({"id": other_id, "used_percent": 100,
                                           "resets_at": utc_iso(NOW + timedelta(days=2)),
                                           "window_minutes": duration})
                    self.save(raw, provider)
                    row = self.provider(provider)
                    self.assertEqual([alert["kind"] for alert in row["alerts"]], ["quota_reached"])
                    self.assertEqual(row["next_action"], "quota_reached")
                    raw["windows"][0]["used_percent"] = 90
                    self.save(raw, provider)
                    self.assertEqual({alert["kind"] for alert in self.provider(provider)["alerts"]},
                                     {"quota_low", "quota_reached"})

    def test_expired_exhausted_window_does_not_block_active_use_before_reset(self):
        for provider, other_id in (("claude", "seven_day"), ("codex", "secondary")):
            with self.subTest(provider=provider):
                raw = snapshot(provider, used=25, reset_hours=1)
                raw["windows"].append({"id": other_id, "used_percent": 100,
                                       "resets_at": utc_iso(NOW - timedelta(seconds=1)),
                                       "window_minutes": 10080})
                self.save(raw, provider)
                row = self.provider(provider)
                self.assertEqual([alert["kind"] for alert in row["alerts"]], ["use_before_reset"])

    def test_stale_snapshot_has_no_alerts_and_retains_observed_usage(self):
        raw = snapshot(used=90)
        raw["collected_at"] = utc_iso(NOW - timedelta(seconds=901))
        self.save(raw)
        row = self.provider()
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["windows"][0]["used_percent"], 90)
        self.assertEqual(row["alerts"], [])
        self.assertEqual(row["next_action"], "refresh_usage")

    def test_expired_window_retains_history_without_remaining_or_alerts(self):
        for hours in (-1, 0):
            self.save(snapshot(reset_hours=hours))
            row = self.provider()
            self.assertEqual(row["windows"][0]["status"], "expired")
            self.assertIsNone(row["windows"][0]["remaining_percent"])
            self.assertEqual(row["windows"][0]["used_percent"], 40)
            self.assertEqual(row["alerts"], [])

    def test_invalid_percentage_never_becomes_zero_or_alert(self):
        for value in (-1, float("nan"), float("inf"), True, "85", None, {}):
            with self.subTest(value=value):
                self.save(snapshot(used=value))
                row = self.provider()
                self.assertIsNone(row["windows"][0]["used_percent"])
                self.assertIsNone(row["windows"][0]["remaining_percent"])
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["alerts"], [])
                json.dumps(row, allow_nan=False)

    def test_missing_reset_is_unknown_and_suppresses_all_alerts(self):
        for value in (None, "invalid", "2026-10-04T20:00:00", True):
            raw = snapshot(used=100)
            raw["windows"][0]["resets_at"] = value
            self.save(raw)
            row = self.provider()
            self.assertIsNone(row["windows"][0]["resets_at"])
            self.assertEqual(row["alerts"], [])

    def test_invalid_metadata_and_far_future_are_safe_errors(self):
        for field, value in (("schema_version", "wrong"), ("provider", "codex"),
                             ("source", "PRIVATE_MARKER"), ("collected_at", "invalid"),
                             ("collected_at", utc_iso(NOW + timedelta(seconds=61)))):
            with self.subTest(field=field):
                raw = snapshot()
                raw[field] = value
                self.save(raw)
                row = self.provider()
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["source"], "not_connected")
                self.assertIsNone(row["collected_at"])
                self.assertNotIn("PRIVATE_MARKER", json.dumps(row))
        raw = snapshot()
        raw["collected_at"] = utc_iso(NOW + timedelta(seconds=60))
        self.save(raw)
        self.assertEqual(self.provider()["status"], "ready")

    def test_non_utc_timestamps_normalize(self):
        raw = snapshot()
        raw["collected_at"] = "2026-10-05T01:00:00+07:00"
        raw["windows"][0]["resets_at"] = "2026-10-05T03:00:00+07:00"
        self.save(raw)
        row = self.provider()
        self.assertEqual(row["collected_at"], utc_iso(NOW))
        self.assertEqual(row["windows"][0]["resets_at"], "2026-10-04T20:00:00Z")

    def test_codex_labels_follow_validated_duration_for_both_windows(self):
        for window_id, fallback in (("primary", "รอบหลัก"), ("secondary", "รอบรอง")):
            for minutes, expected in ((300, "รอบ 5 ชั่วโมง"), (10080, "รอบ 7 วัน"),
                                      (120, "รอบ 2 ชั่วโมง"), (90, "รอบ 90 นาที"),
                                      (None, fallback), (0, fallback), (-1, fallback),
                                      (True, fallback), ("300", fallback), (300.0, fallback)):
                with self.subTest(window_id=window_id, minutes=minutes):
                    raw = snapshot("codex", used=90, minutes=minutes)
                    raw["windows"][0]["id"] = window_id
                    self.save(raw, "codex")
                    row = self.provider("codex")
                    self.assertEqual(row["windows"][0]["label"], expected)
                    self.assertIn(expected, row["alerts"][0]["message"])
                    self.assertNotIn("PRIVATE_MARKER", json.dumps(row))

    def test_duplicate_and_unknown_window_ids_are_discarded(self):
        raw = snapshot()
        raw["windows"].extend([dict(raw["windows"][0]), {"id": "PRIVATE_MARKER", "used_percent": 100}, None])
        self.save(raw)
        row = self.provider()
        self.assertEqual(row["windows"], [])
        self.assertEqual(row["status"], "error")
        self.assertNotIn("PRIVATE_MARKER", json.dumps(row))

    def test_partial_windows_and_session_have_explicit_unknowns(self):
        raw = snapshot()
        raw["session"] = {"estimated_cost_usd": -1, "input_tokens": True, "output_tokens": 2.5}
        raw["windows"].append({"id": "seven_day", "used_percent": None})
        self.save(raw)
        row = self.provider()
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["session"], {"estimated_cost_usd": None, "input_tokens": None, "output_tokens": None})
        self.assertIsNone(row["windows"][1]["used_percent"])

    def test_no_windows_or_malformed_windows_cannot_be_ready(self):
        for windows in ([], None, {}, "PRIVATE_MARKER", [None] * 21):
            raw = snapshot()
            raw["windows"] = windows
            self.save(raw)
            self.assertEqual(self.provider()["status"], "error")

    def test_plan_labels_are_allowlisted(self):
        for provider, plan, expected in (("claude", "pro", "Pro"), ("codex", "Pro Lite", "Pro Lite"),
                                         ("codex", "Pro Max", "Pro Max"), ("claude", "PRIVATE_MARKER", None)):
            raw = snapshot(provider)
            raw["plan"] = plan
            self.save(raw, provider)
            self.assertEqual(self.provider(provider)["plan"], expected)

    def test_fees_from_settings_env_override_and_missing_total(self):
        self.save({"monthly_fees_usd": {"claude": 31, "codex": 17}, "private": "PRIVATE_MARKER"}, "settings")
        self.assertEqual(self.store.snapshot(now=NOW)["summary"]["monthly_fee_usd"], 48)
        self.store.environ = {"AI_USAGE_CODEX_MONTHLY_FEE_USD": "9"}
        self.assertEqual(self.store.snapshot(now=NOW)["summary"]["monthly_fee_usd"], 40)
        for bad in ("NaN", "Infinity", "-1", "invalid", ""):
            self.store.environ = {"AI_USAGE_CODEX_MONTHLY_FEE_USD": bad}
            result = self.store.snapshot(now=NOW)
            self.assertIsNone(result["summary"]["monthly_fee_usd"])
            self.assertFalse(result["summary"]["fees_complete"])
        self.store.environ = {}
        self.save({"monthly_fees_usd": {"claude": True, "codex": "17"}}, "settings")
        self.assertIsNone(self.store.snapshot(now=NOW)["summary"]["monthly_fee_usd"])
        self.assertNotIn("PRIVATE_MARKER", json.dumps(self.store.snapshot(now=NOW)))

    def test_bad_json_duplicate_fields_or_oversized_files_are_safe_errors(self):
        for payload in ("{", "[]", '{"provider":"claude","provider":"codex"}', " " * (MAX_SNAPSHOT_BYTES + 1)):
            (self.path / "claude.json").write_text(payload, encoding="utf-8")
            self.assertEqual(self.provider()["status"], "error")
        self.save(snapshot("codex"), "codex")
        self.assertEqual(self.provider("codex")["status"], "ready")

    def test_symlink_snapshot_directory_and_ancestor_are_never_read(self):
        real = self.path / "real"
        real.mkdir()
        (real / "claude.json").write_text(json.dumps(snapshot()), encoding="utf-8")
        (self.path / "claude.json").symlink_to(real / "claude.json")
        self.assertEqual(self.provider()["status"], "error")
        link = self.path / "link"
        link.symlink_to(real, target_is_directory=True)
        linked = UsageStore(link, environ={}).snapshot(now=NOW)
        self.assertEqual(linked["providers"][0]["status"], "error")
        nested = real / "nested"
        nested.mkdir()
        (nested / "claude.json").write_text(json.dumps(snapshot()), encoding="utf-8")
        ancestor = UsageStore(link / "nested", environ={}).snapshot(now=NOW)
        self.assertEqual(ancestor["providers"][0]["status"], "error")

    def test_fifo_never_blocks(self):
        os.mkfifo(self.path / "claude.json")
        self.assertEqual(self.provider()["status"], "error")

    def test_settings_symlink_does_not_publish_fees(self):
        self.save({"monthly_fees_usd": {"claude": 5, "codex": 7}}, "codex")
        (self.path / "settings.json").symlink_to(self.path / "codex.json")
        self.assertFalse(self.store.snapshot(now=NOW)["summary"]["fees_complete"])

    def test_each_read_updates_time_without_mutating_snapshots(self):
        self.save(snapshot())
        before = (self.path / "claude.json").read_bytes()
        first = self.store.snapshot(now=NOW)
        later = self.store.snapshot(now=NOW + timedelta(seconds=901))
        self.assertNotEqual(first["generated_at"], later["generated_at"])
        self.assertEqual(later["providers"][0]["status"], "stale")
        self.assertEqual((self.path / "claude.json").read_bytes(), before)

    def test_default_state_dir_can_be_configured_without_reading_real_state(self):
        with patch.dict(os.environ, {"AI_USAGE_STATE_DIR": str(self.path)}):
            self.assertEqual(UsageStore().state_dir, self.path)


if __name__ == "__main__":
    unittest.main()


def weekly_snapshot(provider="claude", used=2, reset_hours=24):
    raw = snapshot(provider)
    raw["windows"] = [{"id": "seven_day" if provider == "claude" else "secondary", "used_percent": used,
                       "resets_at": utc_iso(NOW + timedelta(hours=reset_hours)), "window_minutes": 10080}]
    return raw


class SubscriptionRoiTest(unittest.TestCase):
    FEES = {"claude": 3500, "codex": 700, "gemini": 700, "grok": 1000, "muse": 300}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = UsageStore(self.path, environ={})

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, raw, name="settings"):
        (self.path / (name + ".json")).write_text(json.dumps(raw), encoding="utf-8")

    def settings(self, profit=24000, revenue=80000, **extra):
        subs = {key: {"monthly_fee_thb": fee} for key, fee in self.FEES.items()}
        for key, values in extra.items():
            subs[key].update(values)
        return {"subscriptions": subs, "business": {
            "month": "2026-10", "monthly_revenue_thb": revenue, "monthly_profit_before_ai_thb": profit}}

    def test_v1_providers_unchanged_and_all_five_subscriptions_listed(self):
        result = self.store.snapshot(now=NOW)
        self.assertEqual([row["id"] for row in result["providers"]], ["claude", "codex"])
        self.assertEqual([row["id"] for row in result["subscriptions"]], ["claude", "codex", "gemini", "grok", "muse"])
        self.assertEqual([row["has_quota_collector"] for row in result["subscriptions"]], [True, True, False, False, False])
        roi = result["roi"]
        self.assertEqual(roi["verdict"], "incomplete")
        self.assertIsNone(roi["ai_spend_thb"])
        self.assertEqual(roi["missing_fees"], ["claude", "codex", "gemini", "grok", "muse"])
        self.assertEqual((roi["ratio_status"], roi["budget_status"]), ("unknown", "unknown"))

    def test_ratio_and_budget_band(self):
        self.save(self.settings())
        roi = self.store.snapshot(now=NOW)["roi"]
        self.assertEqual(roi["ai_spend_thb"], 6200)
        self.assertEqual(roi["profit_to_ai_ratio"], round(24000 / 6200, 4))
        self.assertEqual((roi["ratio_status"], roi["verdict"]), ("meets_target", "paying_for_itself"))
        self.assertEqual(roi["ai_spend_percent_of_revenue"], 7.75)
        self.assertEqual(roi["budget_status"], "within_band")
        self.assertEqual(roi["budget_band_thb"], {"min": 4000, "max": 8000})
        self.assertEqual(roi["max_ai_spend_by_ratio_thb"], 8000)
        self.assertEqual(roi["month"], "2026-10")
        for profit, revenue, ratio_status, budget in (
            (18000, 200000, "below_target", "under_band"), (18600, 50000, "meets_target", "over_band"),
            (-5000, 62000, "below_target", "within_band"),
        ):
            self.save(self.settings(profit=profit, revenue=revenue))
            roi = self.store.snapshot(now=NOW)["roi"]
            self.assertEqual((roi["ratio_status"], roi["budget_status"]), (ratio_status, budget), (profit, revenue))
        self.assertEqual(roi["verdict"], "not_paying_for_itself")
        self.assertEqual(roi["max_ai_spend_by_ratio_thb"], 0)

    def test_missing_fee_or_profit_never_produces_a_ratio(self):
        raw = self.settings()
        del raw["subscriptions"]["grok"]["monthly_fee_thb"]
        self.save(raw)
        roi = self.store.snapshot(now=NOW)["roi"]
        self.assertEqual((roi["missing_fees"], roi["ai_spend_thb"], roi["profit_to_ai_ratio"]), (["grok"], None, None))
        self.assertIn("Grok", roi["message"])
        raw = self.settings(profit=None)
        self.save(raw)
        roi = self.store.snapshot(now=NOW)["roi"]
        self.assertEqual((roi["verdict"], roi["profit_to_ai_ratio"]), ("incomplete", None))
        self.assertEqual(roi["ai_spend_thb"], 6200)

    def test_env_overrides_and_invalid_values_are_unknown(self):
        self.save(self.settings())
        self.store.environ = {"AI_USAGE_GROK_MONTHLY_FEE_THB": "0", "AI_USAGE_MONTHLY_PROFIT_BEFORE_AI_THB": "-100"}
        roi = self.store.snapshot(now=NOW)["roi"]
        self.assertEqual((roi["ai_spend_thb"], roi["profit_before_ai_thb"]), (5200, -100))
        for bad in ("", "nan", "inf", "-1", "1e13"):
            self.store.environ = {"AI_USAGE_MUSE_MONTHLY_FEE_THB": bad}
            self.assertEqual(self.store.snapshot(now=NOW)["roi"]["missing_fees"], ["muse"], bad)
        self.store.environ = {}
        raw = self.settings(revenue=-1)
        raw["subscriptions"]["gemini"].update(monthly_fee_thb=True, plan="<script>")
        raw["business"]["month"] = "2026-13"
        self.save(raw)
        result = self.store.snapshot(now=NOW)
        gemini = result["subscriptions"][2]
        self.assertEqual((gemini["monthly_fee_thb"], gemini["plan"]), (None, None))
        self.assertEqual((result["roi"]["revenue_thb"], result["roi"]["month"]), (None, None))

    def test_plan_prefers_collected_quota_plan(self):
        self.save(self.settings(claude={"plan": "Max 20x"}, gemini={"plan": "AI Pro"}))
        subs = {row["id"]: row for row in self.store.snapshot(now=NOW)["subscriptions"]}
        self.assertEqual((subs["claude"]["plan"], subs["gemini"]["plan"]), ("Max 20x", "AI Pro"))
        self.save(snapshot(), "claude")
        subs = {row["id"]: row for row in self.store.snapshot(now=NOW)["subscriptions"]}
        self.assertEqual(subs["claude"]["plan"], "Pro")

    def test_near_zero_usage_flags_consider_pausing(self):
        self.save(self.settings(gemini={"active_days_30d": 1}, grok={"active_days_30d": 12},
                                muse={"active_days_30d": 0, "monthly_fee_thb": 0}))
        self.save(weekly_snapshot("claude", used=2, reset_hours=24), "claude")
        self.save(weekly_snapshot("codex", used=2, reset_hours=150), "codex")
        result = self.store.snapshot(now=NOW)
        subs = {row["id"]: row for row in result["subscriptions"]}
        self.assertEqual(subs["claude"]["usage"], {"signal": "quota", "level": "near_zero", "used_percent": 2,
                                                   "active_days_30d": None})
        # Codex weekly window only just reset: too early to call it idle.
        self.assertEqual(subs["codex"]["usage"]["level"], "unknown")
        self.assertEqual(subs["gemini"]["usage"]["signal"], "self_reported")
        self.assertEqual(subs["grok"]["usage"]["level"], "active")
        self.assertFalse(subs["muse"]["consider_pausing"])  # nothing paid, nothing to pause
        self.assertEqual(result["roi"]["consider_pausing"], ["claude", "gemini"])

    def test_measured_quota_beats_self_report_and_stale_is_unknown(self):
        self.save(self.settings(claude={"active_days_30d": 0}))
        self.save(weekly_snapshot("claude", used=40), "claude")
        claude = self.store.snapshot(now=NOW)["subscriptions"][0]
        self.assertEqual((claude["usage"]["signal"], claude["usage"]["level"]), ("quota", "active"))
        stale = weekly_snapshot("claude", used=1)
        stale["collected_at"] = utc_iso(NOW - timedelta(hours=1))
        self.save(stale, "claude")
        claude = self.store.snapshot(now=NOW)["subscriptions"][0]
        self.assertEqual((claude["usage"]["signal"], claude["usage"]["level"]), ("self_reported", "near_zero"))
