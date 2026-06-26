from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from plan_limits import PLAN_LIMITS


REPORT_PATH = Path(__file__).resolve().parent / "plan_limits_report.json"


def check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def main() -> int:
    trial = PLAN_LIMITS["trial"]
    standard = PLAN_LIMITS["standard"]
    premium = PLAN_LIMITS["premium"]

    checks = [
        check(
            "plans_present",
            set(PLAN_LIMITS) == {"trial", "standard", "premium"},
            f"plans={sorted(PLAN_LIMITS)}",
        ),
        check(
            "document_limits_increase",
            trial.document_credits < standard.document_credits < premium.document_credits,
            (
                f"trial={trial.document_credits}, "
                f"standard={standard.document_credits}, "
                f"premium={premium.document_credits}"
            ),
        ),
        check(
            "voice_limits_increase",
            (
                trial.feature_limit("voice_transcription")
                < standard.feature_limit("voice_transcription")
                < premium.feature_limit("voice_transcription")
            ),
            (
                f"trial={trial.feature_limit('voice_transcription')}, "
                f"standard={standard.feature_limit('voice_transcription')}, "
                f"premium={premium.feature_limit('voice_transcription')}"
            ),
        ),
        check(
            "ai_daily_limits_increase",
            trial.ai_per_day < standard.ai_per_day < premium.ai_per_day,
            (
                f"trial={trial.ai_per_day}, standard={standard.ai_per_day}, "
                f"premium={premium.ai_per_day}"
            ),
        ),
        check(
            "trial_email_blocked",
            not trial.feature_enabled("email_delivery"),
            "Trial must not include external email delivery.",
        ),
        check(
            "standard_email_enabled",
            standard.feature_enabled("email_delivery")
            and standard.feature_limit("email_delivery") > 0,
            f"limit={standard.feature_limit('email_delivery')}",
        ),
        check(
            "standard_sms_blocked",
            not standard.feature_enabled("sms_delivery"),
            "SMS delivery is reserved for Premium.",
        ),
        check(
            "premium_sms_enabled",
            premium.feature_enabled("sms_delivery")
            and premium.feature_limit("sms_delivery") > 0,
            f"limit={premium.feature_limit('sms_delivery')}",
        ),
    ]

    passed = all(bool(item["passed"]) for item in checks)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": passed,
        "plans": {
            name: {
                "document_credits": limits.document_credits,
                "ai_per_minute": limits.ai_per_minute,
                "ai_per_hour": limits.ai_per_hour,
                "ai_per_day": limits.ai_per_day,
                "features": sorted(limits.enabled_features),
                "feature_limits": dict(limits.feature_limits),
            }
            for name, limits in PLAN_LIMITS.items()
        },
        "checks": checks,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    for item in checks:
        label = "PASS" if item["passed"] else "FAIL"
        print(f"[{label}] {item['name']}: {item['detail']}")
    print()
    print("OVERALL VERDICT:", "PASS" if passed else "FAIL")
    print("Report:", REPORT_PATH)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
