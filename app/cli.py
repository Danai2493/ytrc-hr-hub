"""Command line for IT: python -m app.cli <command>

  migrate             create or upgrade the database tables (Alembic)
  seed-reference      load org structure, shifts, calendars, grades (no personal data)
  seed-demo           load SAMPLE people and six weeks of OT (demo databases only)
  create-admin NAME   create an app-only admin login and print a temporary password
  run-jobs            apply future-dated job events and run the data health checks (run nightly)
"""
from __future__ import annotations

import secrets
import sys
from datetime import date

from .db import Base, engine, session_scope


def migrate() -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")


def create_all() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(engine)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    import app.services.ot  # noqa: F401  (approval hooks)
    import app.services.people  # noqa: F401

    if cmd == "migrate":
        migrate()
    elif cmd == "create-all":
        create_all()
    elif cmd == "seed-reference":
        from .seed.reference import seed_reference

        with session_scope() as db:
            seed_reference(db)
    elif cmd == "seed-demo":
        from .seed.demo import seed_demo
        from .seed.reference import seed_reference

        with session_scope() as db:
            seed_reference(db)
            print(seed_demo(db))
    elif cmd == "create-admin":
        from .models import User
        from .security import hash_password

        name = argv[1] if len(argv) > 1 else "it.admin"
        pw = secrets.token_urlsafe(10) + "9z"
        with session_scope() as db:
            db.add(User(username=name, display_name=name, roles=["admin"], password_hash=hash_password(pw)))
        print(f"Admin '{name}' created. Temporary password: {pw}")
    elif cmd == "run-jobs":
        from .services import health
        from .services.people import apply_due_events

        with session_scope() as db:
            n = apply_due_events(db, date.today())
            res = health.run(db)
        print(f"{n} events applied; data health: {res}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
