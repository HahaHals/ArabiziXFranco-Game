"""Owner stats (spec section 6)."""

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Answer, Player, Sentence, SentenceStatus


def compute(db: Session, top: int = 10) -> dict:
    per_word = dict(db.execute(
        select(Answer.arabic, func.count()).where(Answer.joined.is_(False)).group_by(Answer.arabic)
    ).all())
    # Words in sentences players can currently get, including ones with no answers yet.
    live_words: Counter = Counter()
    for words in db.scalars(select(Sentence.words).where(Sentence.status == SentenceStatus.active)):
        live_words.update(set(words))
    least = sorted(live_words, key=lambda w: (per_word.get(w, 0), w))[:top]
    return {
        "players": db.scalar(select(func.count(func.distinct(Answer.player_id)))) or 0,
        "players_total": db.scalar(select(func.count(Player.id))) or 0,
        "answers": db.scalar(select(func.count(Answer.id))) or 0,
        "words": len(per_word),
        "variants": db.scalar(
            select(func.count()).select_from(select(Answer.arabic, Answer.franco).distinct().subquery())
        ) or 0,
        "sentences_active": db.scalar(
            select(func.count(Sentence.id)).where(Sentence.status == SentenceStatus.active)
        ) or 0,
        "most": sorted(per_word.items(), key=lambda kv: -kv[1])[:top],
        "least": [(w, per_word.get(w, 0)) for w in least],
    }
