"""Owner panel (spec section 6). Most sections are placeholders until their build step."""

import hmac
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import auth, exports, flags, generation, reports, sentences, settings_store, stats
from app import db as dbmod
from app.config import SETUP_CODE_MIN_LENGTH, get_config
from app.db import get_db
from sqlalchemy import delete, func, select

from app.models import (
    Answer, FlagReview, GenerationBatch, Owner, Player, ReportReason, Sentence, SentenceStatus,
    SentenceView,
    TargetWord, Theme, ViewOutcome,
)
from app.templating import templates
from app.text import MAX_DISPLAY_NAME, clean_display_name, non_arabic_words, normalize_sentence, split_words

router = APIRouter(prefix="/owner")

# slug -> (title, what it will do, build step that fills it in)
SECTIONS: dict[str, tuple[str, str, int]] = {
    "stats": ("Stats", "Players, answers, words and variants; most- and least-covered words.", 3),
    "sentences": ("Own sentences", "Add one sentence or bulk paste, with optional English gloss.", 3),
    "players": ("Players", "Contribution counts; delete all rows from a junk player.", 4),
    "reports": ("Reports queue", "Fix and re-release, dismiss, or delete reported sentences.", 5),
    "deleted": ("Deleted sentences", "Restore a deleted sentence to rotation.", 5),
    "themes": ("Themes", "Add, edit, activate or deactivate themes.", 6),
    "target-words": ("Target words", "Words to collect variants for, with progress.", 6),
    "generation": ("Generation", "Trigger a batch, require-review toggle, review queue.", 6),
    "flagged": ("Flagged answers", "Keep or exclude answers the junk filter flagged.", 7),
    "export": ("Export", "Raw answers and grouped variants as CSV or JSONL.", 8),
    "settings": ("Settings", "Players per sentence, report limit, generation and junk-filter numbers.", 2),
}
BUILT = set(SECTIONS)


def _setup_enabled() -> bool:
    return len(get_config().setup_code) >= SETUP_CODE_MIN_LENGTH


def _setup_page(request, error=None, username="", status_code=200):
    return templates.TemplateResponse(
        request, "owner/setup.html",
        {"enabled": _setup_enabled(), "min_length": SETUP_CODE_MIN_LENGTH,
         "error": error, "username": username},
        status_code=status_code,
    )


@router.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    # Only exists until the first owner account is created.
    if auth.owners_exist(db):
        return RedirectResponse("/owner/login", status_code=303)
    return _setup_page(request)


@router.post("/setup")
def setup_submit(
    request: Request,
    setup_code: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    repeat: str = Form(""),
    db: Session = Depends(get_db),
):
    if auth.owners_exist(db):
        return RedirectResponse("/owner/login", status_code=303)
    if not _setup_enabled():
        return _setup_page(request, status_code=403)
    if not hmac.compare_digest(setup_code.encode(), get_config().setup_code.encode()):
        return _setup_page(request, "The setup code is wrong.", username, 403)
    if password != repeat:
        return _setup_page(request, "The passwords don't match.", username, 422)
    try:
        owner = auth.create_owner(db, username, password)
    except auth.OwnerError as e:
        return _setup_page(request, str(e), username, 422)
    auth.login(request, owner)
    return RedirectResponse("/owner", status_code=303)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get(auth.SESSION_KEY) is not None:
        return RedirectResponse("/owner", status_code=303)
    if not auth.owners_exist(db):
        return RedirectResponse("/owner/setup", status_code=303)
    return templates.TemplateResponse(request, "owner/login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    owner = auth.authenticate(db, username, password)
    if owner is None:
        return templates.TemplateResponse(
            request,
            "owner/login.html",
            {"error": "Wrong username or password.", "username": username},
            status_code=401,
        )
    auth.login(request, owner)
    return RedirectResponse("/owner", status_code=303)


@router.post("/logout")
def logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/owner/login", status_code=303)


@router.get("")
def panel(request: Request, owner: Owner = Depends(auth.require_owner)):
    return templates.TemplateResponse(
        request, "owner/panel.html", {"owner": owner, "sections": SECTIONS, "built": BUILT}
    )


def _settings_page(request, owner, values, errors, saved=False, status_code=200):
    groups: dict[str, list] = {}
    for key, spec in settings_store.SPECS.items():
        groups.setdefault(spec.group, []).append((key, spec))
    return templates.TemplateResponse(
        request,
        "owner/settings.html",
        {"owner": owner, "sections": SECTIONS, "slug": "settings", "groups": groups,
         "values": values, "errors": errors, "saved": saved},
        status_code=status_code,
    )


def _account_page(request, owner, error=None, changed=False, status_code=200):
    return templates.TemplateResponse(
        request, "owner/account.html",
        {"owner": owner, "sections": SECTIONS, "slug": "account", "error": error,
         "changed": changed, "min_length": auth.MIN_PASSWORD_LENGTH},
        status_code=status_code,
    )


@router.get("/account")
def account_page(request: Request, owner: Owner = Depends(auth.require_owner)):
    return _account_page(request, owner, changed=request.query_params.get("changed") == "1")


@router.post("/account")
def account_submit(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    repeat: str = Form(""),
    owner: Owner = Depends(auth.require_owner),
    db: Session = Depends(get_db),
):
    try:
        auth.change_password(db, owner, current, new, repeat)
    except auth.OwnerError as e:
        return _account_page(request, owner, str(e), status_code=422)
    return RedirectResponse("/owner/account?changed=1", status_code=303)


def _sentences_page(request, owner, db, result=None, form=None, status_code=200):
    answer_counts = (
        select(Answer.sentence_id, func.count(func.distinct(Answer.player_id)).label("n"))
        .group_by(Answer.sentence_id).subquery()
    )
    recent = db.execute(
        select(Sentence, func.coalesce(answer_counts.c.n, 0))
        .outerjoin(answer_counts, answer_counts.c.sentence_id == Sentence.id)
        .order_by(Sentence.id.desc()).limit(100)
    ).all()
    total = db.scalar(select(func.count(Sentence.id)))
    return templates.TemplateResponse(
        request, "owner/sentences.html",
        {"owner": owner, "sections": SECTIONS, "slug": "sentences", "recent": recent,
         "total": total, "result": result, "form": form or {}},
        status_code=status_code,
    )


@router.get("/sentences")
def sentences_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return _sentences_page(request, owner, db)


@router.post("/sentences")
def sentences_submit(
    request: Request,
    arabic: str = Form(""),
    english: str = Form(""),
    bulk: str = Form(""),
    owner: Owner = Depends(auth.require_owner),
    db: Session = Depends(get_db),
):
    if bulk.strip():
        items = sentences.parse_bulk(bulk)
    elif arabic.strip():
        items = [(arabic, english)]
    else:
        return _sentences_page(request, owner, db, status_code=422,
                               result=sentences.AddResult(invalid=[("", "Type or paste a sentence first")]))
    result = sentences.add_sentences(db, items)
    # Keep what was typed if nothing got in, so it can be fixed.
    form = {} if result.added else {"arabic": arabic, "english": english, "bulk": bulk}
    return _sentences_page(request, owner, db, result=result, form=form)


def _player_rows(db, limit=500):
    answers = select(Answer.player_id, func.count().label("n")).group_by(Answer.player_id).subquery()
    sentences_done = (
        select(SentenceView.player_id, func.count().label("n"))
        .where(SentenceView.outcome == ViewOutcome.answered)
        .group_by(SentenceView.player_id).subquery()
    )
    return db.execute(
        select(Player, func.coalesce(answers.c.n, 0), func.coalesce(sentences_done.c.n, 0))
        .outerjoin(answers, answers.c.player_id == Player.id)
        .outerjoin(sentences_done, sentences_done.c.player_id == Player.id)
        .order_by(func.coalesce(answers.c.n, 0).desc(), Player.created_at.desc())
        .limit(limit)
    ).all()


@router.get("/players")
def players_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request, "owner/players.html",
        {"owner": owner, "sections": SECTIONS, "slug": "players", "rows": _player_rows(db),
         "total": db.scalar(select(func.count(Player.id))),
         "deleted": request.query_params.get("deleted"), "renamed": request.query_params.get("renamed"),
         "error": None, "max_name": MAX_DISPLAY_NAME},
    )


@router.post("/players/{player_id}/name")
def player_rename(
    player_id: str, request: Request, display_name: str = Form(""),
    owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db),
):
    """Rename or clear a player's leaderboard name (e.g. a rude one). Empty clears it."""
    player = db.get(Player, player_id) or _404()
    name = clean_display_name(display_name)
    if len(name) > MAX_DISPLAY_NAME:
        return templates.TemplateResponse(
            request, "owner/players.html",
            {"owner": owner, "sections": SECTIONS, "slug": "players", "rows": _player_rows(db),
             "total": db.scalar(select(func.count(Player.id))), "deleted": None, "renamed": None,
             "error": f"Names must be under {MAX_DISPLAY_NAME} characters.", "max_name": MAX_DISPLAY_NAME},
            status_code=422,
        )
    player.display_name = name or None
    db.commit()
    return RedirectResponse(f"/owner/players?renamed={player_id}", status_code=303)


@router.get("/players/{player_id}/delete")
def player_delete_confirm(
    player_id: str, request: Request,
    owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db),
):
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(404)
    n = db.scalar(select(func.count()).where(Answer.player_id == player_id))
    return templates.TemplateResponse(
        request, "owner/player_delete.html",
        {"owner": owner, "sections": SECTIONS, "slug": "players", "player": player, "answers": n},
    )


@router.post("/players/{player_id}/delete")
def player_delete(
    player_id: str, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    # Answers, views and reports go with the player (ON DELETE CASCADE).
    if db.execute(delete(Player).where(Player.id == player_id)).rowcount == 0:
        raise HTTPException(404)
    db.commit()
    return RedirectResponse(f"/owner/players?deleted={player_id}", status_code=303)


REASON_LABELS = {
    ReportReason.misspelled: "Misspelled",
    ReportReason.not_egyptian: "Not Egyptian Arabic",
    ReportReason.inappropriate: "Inappropriate",
    ReportReason.not_understood: "Doesn't make sense",
}


def _reports_page(request, owner, db, errors=None, forms=None, done=None, status_code=200):
    pulled, live = reports.queue(db)
    answers = dict(db.execute(
        select(Answer.sentence_id, func.count(func.distinct(Answer.player_id))).group_by(Answer.sentence_id)
    ).all())
    return templates.TemplateResponse(
        request, "owner/reports.html",
        {"owner": owner, "sections": SECTIONS, "slug": "reports", "pulled": pulled, "live": live,
         "answers": answers, "labels": REASON_LABELS, "errors": errors or {}, "forms": forms or {},
         "done": done},
        status_code=status_code,
    )


@router.get("/reports")
def reports_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return _reports_page(request, owner, db, done=request.query_params.get("done"))


def _sentence_or_404(db, sentence_id: int) -> Sentence:
    sentence = db.get(Sentence, sentence_id)
    if sentence is None:
        raise HTTPException(404)
    return sentence


@router.post("/sentences/{sentence_id}/fix")
def sentence_fix(
    sentence_id: int, request: Request, arabic: str = Form(""), english: str = Form(""),
    owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db),
):
    sentence = _sentence_or_404(db, sentence_id)
    try:
        new = reports.fix(db, sentence, arabic, english)
    except reports.ReportError as e:
        return _reports_page(request, owner, db, errors={sentence_id: str(e)},
                             forms={sentence_id: (arabic, english)}, status_code=422)
    done = "fixed" if new.id == sentence.id else "replaced"
    return RedirectResponse(f"/owner/reports?done={done}", status_code=303)


@router.post("/sentences/{sentence_id}/dismiss")
def sentence_dismiss(
    sentence_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    reports.dismiss(db, _sentence_or_404(db, sentence_id))
    return RedirectResponse("/owner/reports?done=dismissed", status_code=303)


@router.post("/sentences/{sentence_id}/delete")
def sentence_delete(
    sentence_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    reports.delete(db, _sentence_or_404(db, sentence_id))
    return RedirectResponse("/owner/reports?done=deleted", status_code=303)


@router.get("/deleted")
def deleted_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    rows = db.scalars(
        select(Sentence).where(Sentence.status == SentenceStatus.deleted).order_by(Sentence.id.desc())
    ).all()
    return templates.TemplateResponse(
        request, "owner/deleted.html",
        {"owner": owner, "sections": SECTIONS, "slug": "deleted", "rows": rows,
         "restored": request.query_params.get("restored")},
    )


@router.post("/sentences/{sentence_id}/restore")
def sentence_restore(
    sentence_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    try:
        reports.restore(db, _sentence_or_404(db, sentence_id))
    except reports.ReportError:
        raise HTTPException(409) from None
    return RedirectResponse("/owner/deleted?restored=1", status_code=303)


# --- Themes (spec 4.3) ---

def _themes_page(request, owner, db, error=None, form=None, done=None, status_code=200):
    counts = dict(db.execute(
        select(Sentence.theme_id, func.count()).where(Sentence.theme_id.is_not(None)).group_by(Sentence.theme_id)
    ).all())
    themes = db.scalars(select(Theme).order_by(Theme.active.desc(), Theme.name)).all()
    return templates.TemplateResponse(
        request, "owner/themes.html",
        {"owner": owner, "sections": SECTIONS, "slug": "themes", "themes": themes, "counts": counts,
         "error": error, "form": form or {}, "done": done},
        status_code=status_code,
    )


@router.get("/themes")
def themes_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return _themes_page(request, owner, db, done=request.query_params.get("done"))


@router.post("/themes")
def theme_save(
    request: Request, theme_id: int | None = Form(None), name: str = Form(""),
    description: str = Form(""), owner: Owner = Depends(auth.require_owner),
    db: Session = Depends(get_db),
):
    name, description = " ".join(name.split()), description.strip()
    form = {"theme_id": theme_id, "name": name, "description": description}
    if not name:
        return _themes_page(request, owner, db, "Give the theme a name.", form, status_code=422)
    clash = db.scalar(select(Theme).where(func.lower(Theme.name) == name.lower(), Theme.id != (theme_id or 0)))
    if clash:
        return _themes_page(request, owner, db, f'There\'s already a theme called "{clash.name}".', form, status_code=422)
    if theme_id:
        theme = db.get(Theme, theme_id) or _404()
        theme.name, theme.description = name, description
    else:
        db.add(Theme(name=name, description=description, active=True))
    db.commit()
    return RedirectResponse(f"/owner/themes?done={'saved' if theme_id else 'added'}", status_code=303)


@router.post("/themes/{theme_id}/toggle")
def theme_toggle(theme_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)):
    theme = db.get(Theme, theme_id) or _404()
    theme.active = not theme.active
    db.commit()
    return RedirectResponse("/owner/themes", status_code=303)


def _404():
    raise HTTPException(404)


# --- Target words (spec 4.4) ---

@router.get("/target-words")
def target_words_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request, "owner/target_words.html",
        {"owner": owner, "sections": SECTIONS, "slug": "target-words",
         "rows": generation.target_word_progress(db),
         "threshold": settings_store.get(db, "target_word_answer_threshold"),
         "added": request.query_params.get("added"), "skipped": request.query_params.get("skipped")},
    )


@router.post("/target-words")
def target_words_add(
    words: str = Form(""), owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    existing = set(db.scalars(select(TargetWord.word)))
    added = skipped = 0
    for word in split_words(words):
        if word in existing or non_arabic_words([word]):
            skipped += 1
            continue
        existing.add(word)
        db.add(TargetWord(word=word, active=True))
        added += 1
    db.commit()
    return RedirectResponse(f"/owner/target-words?added={added}&skipped={skipped}", status_code=303)


@router.post("/target-words/{word_id}/remove")
def target_word_remove(word_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)):
    db.delete(db.get(TargetWord, word_id) or _404())
    db.commit()
    return RedirectResponse("/owner/target-words", status_code=303)


# --- Generation and review queue (spec 4.2) ---

def _generation_page(request, owner, db, errors=None, forms=None, done=None, status_code=200):
    pending = db.scalars(
        select(Sentence).where(Sentence.status == SentenceStatus.pending_review)
        .order_by(Sentence.batch_id.desc(), Sentence.id)
    ).all()
    by_batch: dict = {}
    for s in pending:
        by_batch.setdefault(s.batch_id, []).append(s)
    batches = {b.id: b for b in db.scalars(select(GenerationBatch).where(GenerationBatch.id.in_(list(by_batch))))}
    recent = db.execute(
        select(GenerationBatch, func.count(Sentence.id))
        .outerjoin(Sentence, Sentence.batch_id == GenerationBatch.id)
        .group_by(GenerationBatch.id).order_by(GenerationBatch.id.desc()).limit(10)
    ).all()
    cfg = generation.get_config()
    return templates.TemplateResponse(
        request, "owner/generation.html",
        {"owner": owner, "sections": SECTIONS, "slug": "generation", "status": generation.status,
         "has_key": bool(cfg.anthropic_api_key), "model": cfg.anthropic_model,
         "settings": settings_store.get_all(db), "pool": generation.unseen_pool_size(db),
         "by_batch": by_batch, "batches": batches, "recent": recent, "themes": {t.id: t.name for t in db.scalars(select(Theme))},
         "errors": errors or {}, "forms": forms or {}, "done": done, "now": time.time()},
        status_code=status_code,
    )


@router.get("/generation")
def generation_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return _generation_page(request, owner, db, done=request.query_params.get("done"))


@router.post("/generation/run")
def generation_run(background: BackgroundTasks, owner: Owner = Depends(auth.require_owner)):
    if generation.status.running:
        return RedirectResponse("/owner/generation?done=busy", status_code=303)
    background.add_task(generation.run_and_record, dbmod.SessionLocal, owner.username)
    return RedirectResponse("/owner/generation?done=started", status_code=303)


@router.post("/generation/review-setting")
def generation_review_setting(
    require_review: str | None = Form(None), owner: Owner = Depends(auth.require_owner),
    db: Session = Depends(get_db),
):
    settings_store.set_value(db, "require_review", require_review is not None)
    return RedirectResponse("/owner/generation?done=setting", status_code=303)


def _pending_or_404(db, sentence_id) -> Sentence:
    s = db.get(Sentence, sentence_id)
    if s is None or s.status != SentenceStatus.pending_review:
        raise HTTPException(404)
    return s


@router.post("/generation/{sentence_id}/approve")
def pending_approve(sentence_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)):
    _pending_or_404(db, sentence_id).status = SentenceStatus.active
    db.commit()
    return RedirectResponse("/owner/generation?done=approved", status_code=303)


@router.post("/generation/batch/{batch_id}/approve")
def batch_approve(batch_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)):
    pending = db.scalars(select(Sentence).where(
        Sentence.batch_id == batch_id, Sentence.status == SentenceStatus.pending_review)).all()
    for s in pending:
        s.status = SentenceStatus.active
    db.commit()
    return RedirectResponse("/owner/generation?done=approved_all", status_code=303)


@router.post("/generation/{sentence_id}/delete")
def pending_delete(sentence_id: int, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)):
    _pending_or_404(db, sentence_id).status = SentenceStatus.deleted
    db.commit()
    return RedirectResponse("/owner/generation?done=deleted", status_code=303)


@router.post("/generation/{sentence_id}/edit")
def pending_edit(
    sentence_id: int, request: Request, arabic: str = Form(""), english: str = Form(""),
    owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db),
):
    s = _pending_or_404(db, sentence_id)
    arabic, english = " ".join(arabic.split()), english.strip()
    words = split_words(arabic)
    error = None
    if not words:
        error = "The Arabic can't be empty"
    elif foreign := non_arabic_words(words):
        error = f'Not Arabic ("{" ".join(foreign)}")'
    else:
        key = normalize_sentence(arabic)
        if db.scalar(select(Sentence.id).where(Sentence.normalized_text == key, Sentence.id != s.id)):
            error = "That sentence is already in the game"
    if error:
        return _generation_page(request, owner, db, errors={s.id: error},
                                forms={s.id: (arabic, english)}, status_code=422)
    s.text, s.words, s.english, s.normalized_text = arabic, words, english, normalize_sentence(arabic)
    db.commit()
    return RedirectResponse("/owner/generation?done=edited", status_code=303)


# --- Flagged answers (spec 7) ---

@router.get("/flagged")
def flagged_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    rows = db.execute(
        select(Answer, Sentence).join(Sentence, Sentence.id == Answer.sentence_id)
        .where(Answer.flagged.is_(True), Answer.flag_review.is_(None))
        .order_by(Answer.id.desc()).limit(300)
    ).all()
    counts = dict(db.execute(
        select(Answer.flag_review, func.count()).where(Answer.flag_review.is_not(None)).group_by(Answer.flag_review)
    ).all())
    return templates.TemplateResponse(
        request, "owner/flagged.html",
        {"owner": owner, "sections": SECTIONS, "slug": "flagged", "rows": rows,
         "kept": counts.get(FlagReview.kept, 0), "excluded": counts.get(FlagReview.excluded, 0),
         "done": request.query_params.get("done"), "rechecked": request.query_params.get("rechecked")},
    )


@router.post("/flagged/recheck")
def flagged_recheck(owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)):
    n = flags.recheck_all(db)
    return RedirectResponse(f"/owner/flagged?rechecked={n}", status_code=303)


@router.post("/flagged/{answer_id}/{decision}")
def flagged_review(
    answer_id: int, decision: str, owner: Owner = Depends(auth.require_owner),
    db: Session = Depends(get_db),
):
    if decision not in ("keep", "exclude"):
        raise HTTPException(404)
    answer = db.get(Answer, answer_id) or _404()
    flags.review(db, answer, FlagReview.kept if decision == "keep" else FlagReview.excluded)
    return RedirectResponse(f"/owner/flagged?done={decision}", status_code=303)


# --- Export (spec 9) ---

@router.get("/export")
def export_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    def count(include_flagged):
        return db.scalar(
            select(func.count(Answer.id)).join(Sentence, Sentence.id == Answer.sentence_id)
            .where(*exports._filters(include_flagged))
        )
    return templates.TemplateResponse(
        request, "owner/export.html",
        {"owner": owner, "sections": SECTIONS, "slug": "export",
         "clean_count": count(False), "with_flagged": count(True)},
    )


@router.get("/export/{dataset}.{fmt}")
def export_download(
    dataset: str, fmt: str, flagged: bool = False, timestamps: bool = False,
    owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db),
):
    if dataset not in ("answers", "variants") or fmt not in ("csv", "jsonl"):
        raise HTTPException(404)
    if dataset == "answers":
        rows = exports.raw_rows(db, flagged, timestamps)
        columns = exports.RAW_COLUMNS + (["created_at"] if timestamps else [])
    else:
        rows = exports.grouped_rows(db, flagged)
        columns = exports.GROUPED_COLUMNS
    body = exports.to_csv(rows, columns) if fmt == "csv" else exports.to_jsonl(rows)
    stamp = time.strftime("%Y-%m-%d")
    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson; charset=utf-8"
    return StreamingResponse(
        body, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="franco-{dataset}-{stamp}.{fmt}"'},
    )


@router.get("/stats")
def stats_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request, "owner/stats.html",
        {"owner": owner, "sections": SECTIONS, "slug": "stats", "s": stats.compute(db)},
    )


@router.get("/settings")
def settings_page(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    saved = request.query_params.get("saved") == "1"
    return _settings_page(request, owner, settings_store.get_all(db), {}, saved=saved)


@router.post("/settings")
async def settings_submit(
    request: Request, owner: Owner = Depends(auth.require_owner), db: Session = Depends(get_db)
):
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    values, errors = settings_store.parse_form(form)
    if errors:
        # Nothing is saved if any field is wrong.
        return _settings_page(request, owner, values, errors, status_code=422)
    settings_store.set_many(db, values)
    sentences.retire_full_sentences(db, values["max_players"])
    reports.pull_reported_sentences(db, values["report_threshold"])
    flags.recheck_all(db)  # thresholds may have changed
    return RedirectResponse("/owner/settings?saved=1", status_code=303)


@router.get("/{slug}")
def section(slug: str, request: Request, owner: Owner = Depends(auth.require_owner)):
    if slug not in SECTIONS:
        raise HTTPException(404)
    title, description, step = SECTIONS[slug]
    return templates.TemplateResponse(
        request,
        "owner/section.html",
        {"owner": owner, "sections": SECTIONS, "slug": slug, "title": title,
         "description": description, "step": step},
    )
