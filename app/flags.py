"""Flagging suspicious answers for owner review (spec 7)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store
from app.models import Answer, FlagReview
from app.text import flag_reason


def reason_for(db: Session, raw: str, arabic: str) -> str | None:
    s = settings_store.get_all(db)
    return flag_reason(raw, arabic, s["flag_length_ratio"], s["flag_length_slack"], s["flag_repeat_run"])


def recheck_all(db: Session) -> int:
    """Re-run the flag rules on every answer an owner hasn't reviewed yet (e.g. answers saved
    before flagging existed, or after the thresholds changed). Returns how many are flagged."""
    s = settings_store.get_all(db)
    flagged = 0
    for a in db.scalars(select(Answer).where(Answer.flag_review.is_(None))):
        reason = flag_reason(a.franco_raw, a.arabic, s["flag_length_ratio"], s["flag_length_slack"], s["flag_repeat_run"])
        a.flagged, a.flag_reason = reason is not None, reason
        flagged += reason is not None
    db.commit()
    return flagged


def review(db: Session, answer: Answer, decision: FlagReview) -> None:
    answer.flag_review = decision
    if decision is FlagReview.kept:
        answer.flagged = False  # the reason stays as history
    db.commit()
