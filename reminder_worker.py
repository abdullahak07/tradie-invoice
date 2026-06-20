from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    secret = os.getenv("REMINDER_RUN_SECRET", "").strip()

    if not base_url:
        print("[FAIL] PUBLIC_BASE_URL is not configured")
        return 1
    if not secret:
        print("[FAIL] REMINDER_RUN_SECRET is not configured")
        return 1

    url = f"{base_url}/run-reminders"

    try:
        response = httpx.post(
            url,
            headers={"X-Reminder-Secret": secret},
            timeout=120,
        )
    except Exception as exc:
        print(f"[FAIL] Reminder request failed: {exc}")
        return 1

    if response.status_code != 200:
        print(
            f"[FAIL] Reminder endpoint returned {response.status_code}: "
            f"{response.text[:500]}"
        )
        return 1

    print(f"[PASS] Reminder run completed: {response.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
