from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

import bcrypt

from app.db import SessionLocal

# tenant_slug, email, password, role
TEST_USERS = [
    ("default", "test_default@calendo.local", "Test1234!", "staff"),
    ("maria2", "test_maria2@calendo.local", "Test1234!", "staff"),
    ("default", "admin@calendo.local", "Admin1234!", "admin"),
]


def _hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def ensure_test_users() -> None:
    """Create deterministic test tenants + users for debugging.

    Runs ONLY when ENABLE_TEST_USERS=1.
    """
    if os.getenv("ENABLE_TEST_USERS", "").strip() != "1":
        return

    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.user import User
        from app.models.licensing import Plan, Subscription

        plan = db.query(Plan).filter(Plan.code == "TRIAL_7D").first()
        if not plan:
            plan = Plan(code="TRIAL_7D", name="7-day Trial")
            db.add(plan)
            db.commit()
            db.refresh(plan)

        def ensure_tenant(slug: str) -> Tenant:
            t = db.query(Tenant).filter(Tenant.slug == slug).first()
            if not t:
                t = Tenant(slug=slug, name=f"Test Tenant {slug}", is_archived=False)
                db.add(t)
                db.commit()
                db.refresh(t)
            return t

        def ensure_subscription(t: Tenant) -> None:
            sub = (
                db.query(Subscription)
                .filter(Subscription.tenant_id == t.id)
                .order_by(Subscription.ends_at.desc())
                .first()
            )
            if not sub or not getattr(sub, "ends_at", None) or sub.ends_at < datetime.utcnow():
                sub = Subscription(
                    tenant_id=t.id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=datetime.utcnow(),
                    ends_at=datetime.utcnow() + timedelta(days=7),
                )
                db.add(sub)
                db.commit()

        for tenant_slug, email, password, role in TEST_USERS:
            tenant = ensure_tenant(tenant_slug)
            ensure_subscription(tenant)

            email_lc = email.strip().lower()
            user = (
                db.query(User)
                .filter(User.tenant_id == tenant.id, User.email == email_lc)
                .first()
            )
            if not user:
                user = User(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant.id,
                    email=email_lc,
                    password_hash=_hash_pw(password),
                    role=role,
                    is_active=True,
                    must_reset_password=False,
                )
                db.add(user)
                db.commit()
    finally:
        db.close()
