"""Player-facing pages and API (spec section 3)."""

import re
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db as dbmod
from app import flags, generation, ratelimit, reports, settings_store
from app.db import get_db
from app.models import Answer, Player, ReportReason, Sentence, SentenceStatus, SentenceView
from app.sentences import claim_answer, next_sentence_for, retire_if_full
from app.templating import templates
from app.text import clean, franco_error

router = APIRouter()

PLAYER_ID_RE = re.compile(r"^p_[0-9a-f]{8}$")


@router.get("/")
def play_page(request: Request):
    return templates.TemplateResponse(request, "play.html", {})


@router.post("/api/players")
def new_player(request: Request, db: Session = Depends(get_db)) -> dict:
    ratelimit.check(request, None, settings_store.get(db, "rate_limit_per_minute"))
    while True:
        player_id = "p_" + secrets.token_hex(4)
        if db.get(Player, player_id) is None:
            break
    db.add(Player(id=player_id))
    db.commit()
    return {"player_id": player_id}


def _player(db: Session, player_id: str) -> Player:
    player = db.get(Player, player_id) if PLAYER_ID_RE.match(player_id) else None
    if player is None:
        raise HTTPException(404, "Unknown player")
    return player


@router.get("/api/round")
def get_round(player_id: str, background: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
    _player(db, player_id)
    sentence = next_sentence_for(db, player_id, settings_store.get(db, "max_players"))
    # Top up the pool in the background once it runs low (spec 4.2). Runs after the response.
    if generation.should_auto_generate(db):
        background.add_task(generation.run_and_record, dbmod.SessionLocal, "auto")
    if sentence is None:
        return {"sentence": None}
    return {
        "sentence": {
            "id": sentence.id, "text": sentence.text, "english": sentence.english,
            "words": sentence.words,
        }
    }


class Box(BaseModel):
    start: int  # 1-based position of the first word
    end: int  # equal to start unless words were joined
    text: str


class Submission(BaseModel):
    player_id: str
    sentence_id: int
    boxes: list[Box]


class ReportIn(BaseModel):
    player_id: str
    sentence_id: int
    reason: ReportReason
    comment: str | None = None


@router.post("/api/reports")
def submit_report(rep: ReportIn, request: Request, db: Session = Depends(get_db)):
    """Report the current sentence. This skips it; nothing typed for it is saved (spec 3.3)."""
    _player(db, rep.player_id)
    ratelimit.check(request, rep.player_id, settings_store.get(db, "rate_limit_per_minute"))
    sentence = db.get(Sentence, rep.sentence_id)
    if sentence is None:
        raise HTTPException(404, "No such sentence")
    try:
        reports.file_report(
            db, rep.player_id, sentence, rep.reason, rep.comment,
            settings_store.get(db, "report_threshold"), settings_store.get(db, "max_players"),
        )
    except reports.ReportError as e:
        raise HTTPException(409, str(e)) from None
    return {"reported": True}


@router.post("/api/answers")
def submit_answers(sub: Submission, request: Request, db: Session = Depends(get_db)):
    _player(db, sub.player_id)
    ratelimit.check(request, sub.player_id, settings_store.get(db, "rate_limit_per_minute"))
    sentence = db.get(Sentence, sub.sentence_id)
    was_shown = db.get(SentenceView, (sub.player_id, sub.sentence_id)) is not None
    # A sentence that hit the player limit while this player was typing still takes their answer.
    if sentence is None or not (
        sentence.status == SentenceStatus.active
        or (sentence.status == SentenceStatus.retired and was_shown)
    ):
        raise HTTPException(409, "This sentence is no longer available")

    # Boxes must cover every word exactly once, in order.
    boxes = sorted(sub.boxes, key=lambda b: b.start)
    expected = 1
    for box in boxes:
        if box.start != expected or box.end < box.start or box.end > len(sentence.words):
            raise HTTPException(422, "Boxes don't match the sentence's words")
        expected = box.end + 1
    if expected != len(sentence.words) + 1:
        raise HTTPException(422, "Boxes don't match the sentence's words")

    errors = {
        str(b.start): e for b in boxes
        if (e := franco_error(b.text, " ".join(sentence.words[b.start - 1 : b.end])))
    }
    if errors:
        raise HTTPException(422, {"errors": errors})

    try:
        if not claim_answer(db, sub.player_id, sentence.id):
            db.rollback()
            raise HTTPException(409, "You already answered this sentence")
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You already answered this sentence") from None
    for box in boxes:
        arabic = " ".join(sentence.words[box.start - 1 : box.end])
        reason = flags.reason_for(db, box.text, arabic)
        db.add(Answer(
            sentence_id=sentence.id,
            player_id=sub.player_id,
            position_start=box.start,
            position_end=box.end,
            arabic=arabic,
            franco_raw=box.text,
            franco=clean(box.text),
            joined=box.end > box.start,
            flagged=reason is not None,
            flag_reason=reason,
        ))
    retire_if_full(db, sentence, settings_store.get(db, "max_players"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Could not save, try again") from None
    return {"saved": len(boxes)}
