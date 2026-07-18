#!/usr/bin/env python3
"""Create or roll back the email_research_jobs table.

Normal Odysseus startup runs ``Base.metadata.create_all`` and therefore applies
this additive migration automatically. This command exists for operators who
run migrations separately and for an explicit, guarded rollback path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database import EmailResearchJob, SessionLocal, engine


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="drop only email_research_jobs (refuses when it contains rows)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow rollback even when job history exists",
    )
    args = parser.parse_args()

    table = EmailResearchJob.__table__
    if not args.rollback:
        table.create(bind=engine, checkfirst=True)
        print("email_research_jobs migration applied")
        return 0

    db = SessionLocal()
    try:
        count = db.query(EmailResearchJob.id).count()
    finally:
        db.close()
    if count and not args.force:
        parser.error(
            f"email_research_jobs contains {count} row(s); export/backup first "
            "or pass --force"
        )
    table.drop(bind=engine, checkfirst=True)
    print("email_research_jobs migration rolled back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
