"""
Recurring-billing worker — Toss Payments billing-key auto-charges + expiration downgrades.

What it does:
  1) For users with subscription_status='active' AND next_billing_at <= now:
     → attempt the billing-key auto-charge
       - success: refresh plan_expires_at / next_billing_at, reset billing_failed_count=0
       - failure: billing_failed_count++; on the 3rd failure, switch status to past_due
  2) For users with plan_expires_at <= now AND status IN ('canceled', 'past_due') AND plan != 'continental':
     → downgrade plan to continental

Run:
  python -m worker.billing_charger

Scheduled in Railway Cron Job for every day at 03:00 KST (18:00 UTC).
"""
import os
import sys
import time
import traceback
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import db, User, Payment
from app import billing as billing_api
from app.routes import _next_billing_at  # reuse the billing-cycle calculation


_MAX_FAILED_COUNT = 3


def charge_due_subscriptions() -> dict:
    """Auto-charge active users whose next_billing_at <= now."""
    stats = {"total": 0, "paid": 0, "failed": 0, "past_due": 0}

    now = datetime.utcnow()
    users = (
        User.query
        .filter(User.subscription_status == "active",
                User.next_billing_at.isnot(None),
                User.next_billing_at <= now,
                User.billing_key.isnot(None))
        .all()
    )
    stats["total"] = len(users)
    print(f"[CHARGE] active auto-charge candidates: {len(users)}")

    for idx, user in enumerate(users, 1):
        plan  = user.plan
        cycle = user.subscription_cycle or "monthly"
        amount = billing_api.get_price(plan, cycle)

        # Combination missing from the price table → indicates bad data; force-downgrade to continental
        if amount is None:
            print(f"  [{idx}/{len(users)}] SKIP user={user.email}: missing price for plan={plan} cycle={cycle}")
            continue

        order_id = billing_api.make_order_id()
        payment = Payment(
            user_id=user.id,
            plan=plan,
            cycle=cycle,
            amount_krw=amount,
            toss_order_id=order_id,
            charge_type="recurring",
            status="pending",
        )
        db.session.add(payment)
        db.session.commit()

        try:
            res = billing_api.charge_billing_key(
                billing_key=user.billing_key,
                customer_key=user.billing_customer_key or str(user.id),
                amount=amount,
                order_id=order_id,
                order_name=billing_api.order_name(plan, cycle),
                customer_email=user.email,
                customer_name=user.name or user.nickname,
            )
        except billing_api.BillingError as e:
            payment.status = "failed"
            payment.failure_reason = f"{e.code}: {e.message}"
            user.billing_failed_count = (user.billing_failed_count or 0) + 1
            if user.billing_failed_count >= _MAX_FAILED_COUNT:
                user.subscription_status = "past_due"
                user.next_billing_at = None
                stats["past_due"] += 1
                print(f"  [{idx}/{len(users)}] PAST_DUE user={user.email}: {e.code} {e.message} (3 failures)")
            else:
                # Push next_billing_at forward by 1 day so we retry tomorrow
                user.next_billing_at = _next_billing_at_retry(now)
                print(f"  [{idx}/{len(users)}] FAIL user={user.email}: {e.code} {e.message} (retry {user.billing_failed_count}/{_MAX_FAILED_COUNT})")
            db.session.commit()
            stats["failed"] += 1
            continue

        if res.get("status") != "DONE":
            payment.status = "failed"
            payment.failure_reason = f"unexpected status: {res.get('status')}"
            db.session.commit()
            stats["failed"] += 1
            print(f"  [{idx}/{len(users)}] FAIL user={user.email}: unexpected response {res.get('status')}")
            continue

        paid_at = datetime.utcnow()
        payment.status           = "paid"
        payment.toss_payment_key = res.get("paymentKey")
        payment.paid_at          = paid_at

        # Refresh plan dates
        next_at = _next_billing_at(cycle, paid_at)
        user.plan_expires_at      = next_at
        user.next_billing_at      = next_at
        user.billing_failed_count = 0
        db.session.commit()
        stats["paid"] += 1
        print(f"  [{idx}/{len(users)}] PAID  user={user.email} {plan}/{cycle} KRW {amount:,} → next {next_at.date()}")

        time.sleep(0.5)

    return stats


def _next_billing_at_retry(now: datetime) -> datetime:
    """On a failed charge, retry 1 day later."""
    from datetime import timedelta
    return now + timedelta(days=1)


def expire_subscriptions() -> dict:
    """When canceled/past_due users hit their plan_expires_at, downgrade them to continental."""
    stats = {"downgraded": 0}

    now = datetime.utcnow()
    users = (
        User.query
        .filter(User.plan != "continental",
                User.plan_expires_at.isnot(None),
                User.plan_expires_at <= now,
                User.subscription_status.in_(["canceled", "past_due"]))
        .all()
    )
    print(f"[EXPIRE] downgrade candidates: {len(users)}")

    for user in users:
        prev_plan = user.plan
        user.plan = "continental"
        user.subscription_status = None
        user.subscription_cycle  = None
        user.plan_expires_at     = None
        user.next_billing_at     = None
        # Keep billing_key — it can be reused if the user subscribes again
        db.session.commit()
        stats["downgraded"] += 1
        print(f"  DOWNGRADE user={user.email} {prev_plan} → continental")

    return stats


def main() -> int:
    started = datetime.utcnow()
    print(f"[START] billing_charger — {started.isoformat()} UTC")

    app = create_app()
    with app.app_context():
        try:
            charge_stats = charge_due_subscriptions()
            expire_stats = expire_subscriptions()
        except Exception:
            traceback.print_exc()
            return 1

    elapsed = (datetime.utcnow() - started).total_seconds()
    print(
        f"[DONE] elapsed={elapsed:.1f}s | "
        f"charge total={charge_stats['total']} paid={charge_stats['paid']} "
        f"failed={charge_stats['failed']} past_due={charge_stats['past_due']} | "
        f"expire downgraded={expire_stats['downgraded']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
