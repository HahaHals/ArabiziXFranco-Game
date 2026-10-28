"""Reports (spec 3.3) and the owner's reports queue (spec 5)."""

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Report, ReportReason, Sentence, SentenceStatus, SentenceView, ViewOutcome
from app.sentences import retire_if_full
from app.text import non_arabic_words, normalize_sentence, split_words

MAX_COMMENT = 500


class ReportError(ValueError):
    pass


def open_reports(db: Session, sentence_id: int) -> int:
    return db.scalar(
        select(func.count()).where(Report.sentence_id == sentence_id, Report.resolved.is_(False))
    )


def file_report(
    db: Session, player_id: str, sentence: Sentence, reason: ReportReason, comment: str | None,
    report_threshold: int, max_players: int,
) -> None:
    """Record a player's report and skip the sentence for them. Pulls it at the threshold."""
    claimed = db.execute(
        update(SentenceView)
        .where(
            SentenceView.player_id == player_id,
            SentenceView.sentence_id == sentence.id,
            SentenceView.outcome == ViewOutcome.shown,
        )
        .values(outcome=ViewOutcome.reported)
    ).rowcount
    if not claimed:
        db.rollback()
        raise ReportError("You can only report a sentence you're currently on")
    comment = (comment or "").strip()[:MAX_COMMENT] or None
    db.add(Report(sentence_id=sentence.id, player_id=player_id, reason=reason, comment=comment))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ReportError("You already reported this sentence") from None
    if sentence.status == SentenceStatus.active and open_reports(db, sentence.id) >= report_threshold:
        sentence.status = SentenceStatus.reported
    retire_if_full(db, sentence, max_players)
    db.commit()


def pull_reported_sentences(db: Session, report_threshold: int) -> int:
    """Pull every active sentence already at the threshold (e.g. after it was lowered)."""
    counts = (
        select(Report.sentence_id, func.count().label("n"))
        .where(Report.resolved.is_(False)).group_by(Report.sentence_id).subquery()
    )
    ids = db.scalars(
        select(Sentence.id).join(counts, counts.c.sentence_id == Sentence.id)
        .where(Sentence.status == SentenceStatus.active, counts.c.n >= report_threshold)
    ).all()
    if ids:
        db.execute(update(Sentence).where(Sentence.id.in_(ids)).values(status=SentenceStatus.reported))
        db.commit()
    return len(ids)


def queue(db: Session) -> tuple[list, list]:
    """(pulled, still_live): sentences with open reports, each with its open reports."""
    rows = db.execute(
        select(Sentence, Report)
        .join(Report, Report.sentence_id == Sentence.id)
        .where(
            Report.resolved.is_(False),
            Sentence.status.in_([SentenceStatus.reported, SentenceStatus.active]),
        )
        .order_by(Sentence.id, Report.created_at)
    ).all()
    grouped: dict[int, tuple[Sentence, list[Report]]] = {}
    for sentence, report in rows:
        grouped.setdefault(sentence.id, (sentence, []))[1].append(report)
    # Pulled sentences also show up with no open reports if a report was deleted with its player.
    for sentence in db.scalars(select(Sentence).where(Sentence.status == SentenceStatus.reported)):
        grouped.setdefault(sentence.id, (sentence, []))
    items = list(grouped.values())
    pulled = [i for i in items if i[0].status == SentenceStatus.reported]
    live = [i for i in items if i[0].status == SentenceStatus.active]
    return pulled, live


def _resolve(db: Session, sentence: Sentence) -> None:
    db.execute(update(Report).where(Report.sentence_id == sentence.id).values(resolved=True))


def dismiss(db: Session, sentence: Sentence) -> None:
    _resolve(db, sentence)
    sentence.status = SentenceStatus.active
    db.commit()


def delete(db: Session, sentence: Sentence) -> None:
    _resolve(db, sentence)
    sentence.status = SentenceStatus.deleted
    db.commit()


def restore(db: Session, sentence: Sentence) -> None:
    if sentence.status != SentenceStatus.deleted:
        raise ReportError("Only deleted sentences can be restored")
    sentence.status = SentenceStatus.active
    db.commit()


def fix(db: Session, sentence: Sentence, arabic: str, english: str) -> Sentence:
    """Fix and re-release (spec 5).

    Same words (punctuation or harakat aside): the sentence is edited in place and its
    answers stay valid.
    Different words: a new sentence replaces it; the old one and its answers stay as history.
    Returns the sentence that's now in play.
    """
    arabic = " ".join(arabic.split())
    english = english.strip()
    words = split_words(arabic)
    if not words:
        raise ReportError("The Arabic can't be empty")
    if foreign := non_arabic_words(words):
        raise ReportError(f'Not Arabic ("{" ".join(foreign)}")')

    key = normalize_sentence(arabic)
    if key == sentence.normalized_text:
        sentence.text = arabic
        sentence.words = words
        sentence.english = english
        sentence.status = SentenceStatus.active
        _resolve(db, sentence)
        db.commit()
        return sentence

    other = db.scalar(select(Sentence).where(Sentence.normalized_text == key, Sentence.id != sentence.id))
    if other is not None:
        raise ReportError("That sentence is already in the game")
    _resolve(db, sentence)
    sentence.status = SentenceStatus.retired
    new = Sentence(
        text=arabic, normalized_text=key, words=words, english=english, source=sentence.source,
        theme_id=sentence.theme_id, batch_id=sentence.batch_id, status=SentenceStatus.active,
        replaces_id=sentence.id,
    )
    db.add(new)
    db.commit()
    return new
