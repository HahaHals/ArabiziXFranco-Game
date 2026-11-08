"""Dataset exports (spec section 9). Owners only.

The raw answers are the source of truth; the grouped variants are always built from them.
Display names and IPs are never exported.
"""

import csv
import io
import json
from collections.abc import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Answer, FlagReview, Sentence, SentenceStatus

RAW_COLUMNS = [
    "sentence_id", "sentence_text", "english", "position_start", "position_end",
    "arabic", "franco_raw", "franco", "joined", "player_id",
]
GROUPED_COLUMNS = ["arabic", "joined", "variant", "count"]


def _filters(include_flagged: bool) -> list:
    conditions = [
        # Owner-excluded answers never leave; neither do answers on deleted sentences.
        (Answer.flag_review.is_(None)) | (Answer.flag_review != FlagReview.excluded),
        Sentence.status != SentenceStatus.deleted,
    ]
    if not include_flagged:
        conditions.append(Answer.flagged.is_(False))
    return conditions


def raw_rows(db: Session, include_flagged: bool = False, timestamps: bool = False) -> Iterator[dict]:
    query = (
        select(Answer, Sentence.text, Sentence.english)
        .join(Sentence, Sentence.id == Answer.sentence_id)
        .where(*_filters(include_flagged))
        .order_by(Answer.sentence_id, Answer.player_id, Answer.position_start)
    )
    for a, text, english in db.execute(query).yield_per(1000):
        row = {
            "sentence_id": a.sentence_id, "sentence_text": text, "english": english,
            "position_start": a.position_start, "position_end": a.position_end,
            "arabic": a.arabic, "franco_raw": a.franco_raw, "franco": a.franco,
            "joined": a.joined, "player_id": a.player_id,
        }
        if timestamps:
            row["created_at"] = a.created_at.isoformat()
        yield row


def grouped_rows(db: Session, include_flagged: bool = False) -> Iterator[dict]:
    """One row per (Arabic word or joined words, normalized spelling), most common first.
    Joined entries keep their own key and are never merged into their parts."""
    n = func.count().label("n")
    query = (
        select(Answer.arabic, Answer.joined, Answer.franco, n)
        .join(Sentence, Sentence.id == Answer.sentence_id)
        .where(*_filters(include_flagged))
        .group_by(Answer.arabic, Answer.joined, Answer.franco)
        .order_by(n.desc(), Answer.arabic, Answer.franco)
    )
    for arabic, joined, franco, count in db.execute(query):
        yield {"arabic": arabic, "joined": joined, "variant": franco, "count": count}


def to_csv(rows: Iterator[dict], columns: list[str]) -> Iterator[str]:
    """CSV with a UTF-8 BOM so Excel shows the Arabic correctly."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    buf.write("﻿")
    writer.writeheader()
    for i, row in enumerate(rows, 1):
        writer.writerow({k: str(v).lower() if isinstance(v, bool) else v for k, v in row.items()})
        if i % 500 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


def to_jsonl(rows: Iterator[dict]) -> Iterator[str]:
    for row in rows:
        yield json.dumps(row, ensure_ascii=False) + "\n"
