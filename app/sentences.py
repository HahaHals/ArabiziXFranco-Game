"""Adding sentences to the pool, and picking one for a player."""

from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Sentence, SentenceSource, SentenceStatus, SentenceView, ViewOutcome
from app.text import non_arabic_words, normalize_sentence, split_words

MAX_WORDS = 30


@dataclass
class AddResult:
    added: list[Sentence] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (line, reason)


def parse_bulk(text: str) -> list[tuple[str, str]]:
    """One sentence per line, optionally followed by | or a tab and the English gloss."""
    items = []
    for line in text.splitlines():
        if not line.strip():
            continue
        for sep in ("\t", "|"):
            if sep in line:
                arabic, english = line.split(sep, 1)
                break
        else:
            arabic, english = line, ""
        items.append((arabic.strip(), english.strip()))
    return items


def add_sentences(
    db: Session,
    items: list[tuple],
    source: SentenceSource = SentenceSource.owner,
    status: SentenceStatus = SentenceStatus.active,
    batch_id: int | None = None,
) -> AddResult:
    """Add (arabic, english) or (arabic, english, theme_id) items, skipping duplicates of
    existing sentences and of each other."""
    result = AddResult()
    seen: set[str] = set()
    for arabic, english, *rest in items:
        theme_id = rest[0] if rest else None
        arabic = " ".join(arabic.split())
        words = split_words(arabic)
        if not words:
            result.invalid.append((arabic, "No words"))
            continue
        if foreign := non_arabic_words(words):
            reason = "Not Arabic" if len(foreign) == len(words) else f'Not Arabic ("{" ".join(foreign)}")'
            result.invalid.append((arabic, reason))
            continue
        if len(words) > MAX_WORDS:
            result.invalid.append((arabic, f"More than {MAX_WORDS} words"))
            continue
        key = normalize_sentence(arabic)
        exists = db.scalar(select(Sentence.id).where(Sentence.normalized_text == key))
        if key in seen or exists is not None:
            result.duplicates.append(arabic)
            continue
        seen.add(key)
        sentence = Sentence(
            text=arabic, normalized_text=key, words=words, english=english, source=source,
            status=status, theme_id=theme_id, batch_id=batch_id,
        )
        db.add(sentence)
        result.added.append(sentence)
    try:
        db.commit()
    except IntegrityError:
        # Someone added the same sentence at the same moment: fall back to one at a time.
        db.rollback()
        return _add_one_by_one(db, items, source, status, batch_id)
    return result


def _add_one_by_one(db, items, source, status, batch_id) -> AddResult:
    result = AddResult()
    for item in items:
        part = add_sentences(db, [item], source, status, batch_id)
        result.added += part.added
        result.duplicates += part.duplicates
        result.invalid += part.invalid
    return result


def _players_done():
    """Per sentence: how many different players answered or reported it (spec 4.6)."""
    return (
        select(SentenceView.sentence_id, func.count().label("n"))
        .where(SentenceView.outcome.in_([ViewOutcome.answered, ViewOutcome.reported]))
        .group_by(SentenceView.sentence_id)
        .subquery()
    )


def next_sentence_for(db: Session, player_id: str, max_players: int) -> Sentence | None:
    """Pick the player's next sentence and record that it was shown.

    A sentence the player was shown but didn't finish (closed the tab, reloaded) comes back
    first, so reloading can't be used to skip. Otherwise: an active sentence the player hasn't
    answered or reported, under the player limit, with the fewest players so far.
    """
    finished = select(SentenceView.sentence_id).where(
        SentenceView.player_id == player_id, SentenceView.outcome != ViewOutcome.shown
    )
    done = _players_done()
    eligible = (
        select(Sentence)
        .outerjoin(done, done.c.sentence_id == Sentence.id)
        .where(
            Sentence.status == SentenceStatus.active,
            Sentence.id.not_in(finished),
            func.coalesce(done.c.n, 0) < max_players,
        )
    )
    pending = db.scalar(
        eligible.join(
            SentenceView,
            (SentenceView.sentence_id == Sentence.id)
            & (SentenceView.player_id == player_id)
            & (SentenceView.outcome == ViewOutcome.shown),
        ).order_by(SentenceView.created_at.desc()).limit(1)
    )
    if pending is not None:
        return pending
    sentence = db.scalar(eligible.order_by(func.coalesce(done.c.n, 0), func.random()).limit(1))
    if sentence is not None:
        db.add(SentenceView(player_id=player_id, sentence_id=sentence.id, outcome=ViewOutcome.shown))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # another tab recorded it at the same moment
    return sentence


def claim_answer(db: Session, player_id: str, sentence_id: int) -> bool:
    """Mark the sentence answered for this player. False if they already answered or reported it.

    The update only succeeds from "shown", so two submits racing each other can't both win.
    Doesn't commit; the caller saves the answers in the same transaction.
    """
    claimed = db.execute(
        update(SentenceView)
        .where(
            SentenceView.player_id == player_id,
            SentenceView.sentence_id == sentence_id,
            SentenceView.outcome == ViewOutcome.shown,
        )
        .values(outcome=ViewOutcome.answered)
    ).rowcount
    if claimed:
        return True
    if db.get(SentenceView, (player_id, sentence_id)) is not None:
        return False
    db.add(SentenceView(player_id=player_id, sentence_id=sentence_id, outcome=ViewOutcome.answered))
    db.flush()  # raises IntegrityError if a parallel submit inserted it first
    return True


def retire_if_full(db: Session, sentence: Sentence, max_players: int) -> None:
    n = db.scalar(
        select(func.count()).where(
            SentenceView.sentence_id == sentence.id,
            SentenceView.outcome.in_([ViewOutcome.answered, ViewOutcome.reported]),
        )
    )
    if sentence.status == SentenceStatus.active and n >= max_players:
        sentence.status = SentenceStatus.retired


def retire_full_sentences(db: Session, max_players: int) -> int:
    """Retire every active sentence already at the player limit (e.g. after the limit is lowered)."""
    done = _players_done()
    ids = db.scalars(
        select(Sentence.id).join(done, done.c.sentence_id == Sentence.id)
        .where(Sentence.status == SentenceStatus.active, done.c.n >= max_players)
    ).all()
    if ids:
        db.execute(update(Sentence).where(Sentence.id.in_(ids)).values(status=SentenceStatus.retired))
        db.commit()
    return len(ids)
