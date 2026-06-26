from __future__ import annotations

import argparse

from billing import issue_activation_code
from plan_limits import get_plan_limits, normalise_plan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Issue a paid activation code."
    )
    parser.add_argument("--plan", choices=["standard", "premium"], default="standard")
    parser.add_argument(
        "--credits",
        type=int,
        default=None,
        help="Optional document-credit override. Defaults to the selected plan limit.",
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-uses", type=int, default=2)
    args = parser.parse_args()

    plan = normalise_plan(args.plan)
    credits = (
        get_plan_limits(plan).document_credits
        if args.credits is None
        else args.credits
    )
    code = issue_activation_code(
        plan=plan,
        credits=credits,
        days=args.days,
        max_uses=args.max_uses,
    )

    print("[PASS] ACTIVATION CODE CREATED")
    print(f"Plan: {plan.title()}")
    print(f"Document credits: {credits}")
    print(f"Billing days: {args.days}")
    print(f"Allowed channel activations: {args.max_uses}")
    print(f"Code: {code}")
    print()
    print(f"User command: ACTIVATE {code}")


if __name__ == "__main__":
    main()
