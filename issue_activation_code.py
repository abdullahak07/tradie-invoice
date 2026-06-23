from __future__ import annotations

import argparse

from billing import issue_activation_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Issue a paid activation code."
    )
    parser.add_argument("--plan", default="standard")
    parser.add_argument("--credits", type=int, default=150)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-uses", type=int, default=2)
    args = parser.parse_args()

    code = issue_activation_code(
        plan=args.plan,
        credits=args.credits,
        days=args.days,
        max_uses=args.max_uses,
    )

    print("[PASS] ACTIVATION CODE CREATED")
    print(f"Plan: {args.plan}")
    print(f"Credits: {args.credits}")
    print(f"Billing days: {args.days}")
    print(f"Allowed channel activations: {args.max_uses}")
    print(f"Code: {code}")
    print()
    print(f"User command: ACTIVATE {code}")


if __name__ == "__main__":
    main()
