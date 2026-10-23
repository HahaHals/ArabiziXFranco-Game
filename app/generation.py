"""Batch sentence generation with Claude (spec 4.2-4.4, 4.7).

Claude is called server-side only, in the background, never inside a player's request.
A failed call saves nothing; the error is shown in the owner panel and automatic
generation waits FAILURE_COOLDOWN before trying again.
"""

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import settings_store
from app.config import get_config
from app.models import (
    Answer, GenerationBatch, Sentence, SentenceSource, SentenceStatus, SentenceView, TargetWord,
    Theme, ViewOutcome,
)
from app.sentences import add_sentences

log = logging.getLogger(__name__)

FAILURE_COOLDOWN = 30 * 60  # seconds before automatic generation retries after a failure
SUCCESS_COOLDOWN = 2 * 60  # minimum gap between automatic batches
UNDER_COVERED = 25  # how many low-coverage words to steer toward
OVER_COVERED = 15  # how many high-coverage words to steer away from
RECENT_EXAMPLES = 40  # recent sentences shown to Claude so it doesn't repeat them

SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "arabic": {"type": "string"},
                    "english": {"type": "string"},
                    "theme": {"type": "string"},
                },
                "required": ["arabic", "english", "theme"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentences"],
    "additionalProperties": False,
}


class GenerationError(Exception):
    pass


# --- What to ask for ---

@dataclass
class PromptInputs:
    batch_size: int
    themes: list[tuple[str, str]]  # (name, description)
    target_words: list[str]
    under_covered: list[str]
    over_covered: list[str]
    recent: list[str]


def _answers_per_word(db: Session) -> dict[str, int]:
    return dict(db.execute(
        select(Answer.arabic, func.count()).where(Answer.joined.is_(False)).group_by(Answer.arabic)
    ).all())


def target_word_progress(db: Session) -> list[tuple[TargetWord, int]]:
    counts = _answers_per_word(db)
    words = db.scalars(select(TargetWord).order_by(TargetWord.word)).all()
    return [(w, counts.get(w.word, 0)) for w in words]


def gather_inputs(db: Session) -> PromptInputs:
    s = settings_store.get_all(db)
    counts = _answers_per_word(db)
    themes = [(t.name, t.description) for t in db.scalars(
        select(Theme).where(Theme.active.is_(True)).order_by(Theme.name))]
    targets = [
        w.word for w, n in target_word_progress(db)
        if w.active and n < s["target_word_answer_threshold"]
    ]
    # Words that appear in the pool but have the fewest answers so far.
    in_pool: Counter = Counter()
    for words in db.scalars(select(Sentence.words).where(
            Sentence.status.in_([SentenceStatus.active, SentenceStatus.retired]))):
        in_pool.update(set(words))
    under = sorted(in_pool, key=lambda w: (counts.get(w, 0), -in_pool[w]))[:UNDER_COVERED]
    over = [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:OVER_COVERED]]
    recent = db.scalars(select(Sentence.text).order_by(Sentence.id.desc()).limit(RECENT_EXAMPLES)).all()
    return PromptInputs(s["batch_size"], themes, targets, under, over, list(recent))


SYSTEM_PROMPT = """You write example sentences for a word game that collects how Egyptians type Arabic words in Franco (Arabizi, Arabic written with Latin letters and numbers). Players see each sentence in Arabic script and type every word in Franco, so the sentences need to sound exactly like something an Egyptian would say or text.

Rules for every sentence:
- Egyptian colloquial Arabic (عامية مصرية) only. Never Modern Standard Arabic (فصحى): if a word would feel formal or bookish in a Cairo conversation, use the everyday Egyptian word instead.
- Natural, everyday, the kind of thing people say or text. Roughly 4 to 10 words.
- Arabic script only: no English or Latin words, no digits, no emoji, no harakat (tashkeel). Normal punctuation is fine.
- Spelled the way Egyptians usually write colloquial Arabic, consistently.
- Nothing offensive, sexual, political, or religiously sensitive.
- Each sentence different from the others and from the examples you're told already exist.
- A short, natural English translation for each.
- "theme" must be exactly one of the theme names given."""


def build_prompt(inputs: PromptInputs) -> str:
    themes = inputs.themes or [("daily life", "Everyday situations at home, work and out.")]
    lines = [f"Write {inputs.batch_size} new sentences, spread evenly across these themes:"]
    lines += [f"- {name}" + (f": {desc}" if desc else "") for name, desc in themes]
    if inputs.target_words:
        lines += ["", "Work these words in naturally, each in at least one sentence (we need more spellings of them):",
                  "، ".join(inputs.target_words)]
    if inputs.under_covered:
        lines += ["", "These words have few answers so far; use them where they fit naturally:",
                  "، ".join(inputs.under_covered)]
    if inputs.over_covered:
        lines += ["", "These words already have plenty of answers; avoid them unless a sentence really needs one:",
                  "، ".join(inputs.over_covered)]
    if inputs.recent:
        lines += ["", "These sentences already exist; don't repeat them or write near-copies:"]
        lines += [f"- {t}" for t in inputs.recent]
    return "\n".join(lines)


# --- Calling Claude ---

Generator = Callable[[str, str], list[dict]]  # (system, prompt) -> [{"arabic", "english", "theme"}]


def claude_generator(system: str, prompt: str, client: anthropic.Anthropic | None = None) -> list[dict]:
    config = get_config()
    if not config.anthropic_api_key:
        raise GenerationError("No Anthropic API key is set on the server (ANTHROPIC_API_KEY).")
    client = client or anthropic.Anthropic(api_key=config.anthropic_api_key)
    try:
        with client.messages.stream(
            model=config.anthropic_model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            # Low effort keeps the cost down; writing short sentences doesn't need deep thinking.
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise GenerationError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from None
    except anthropic.PermissionDeniedError as e:
        raise GenerationError(f"The API key isn't allowed to do this: {e.message}") from None
    except anthropic.RateLimitError:
        raise GenerationError("Anthropic's rate limit was hit. It will try again later.") from None
    except anthropic.BadRequestError as e:
        # Includes "credit balance is too low".
        raise GenerationError(f"Anthropic refused the request: {e.message}") from None
    except anthropic.APIStatusError as e:
        raise GenerationError(f"Anthropic API error ({e.status_code}). It will try again later.") from None
    except anthropic.APIConnectionError:
        raise GenerationError("Couldn't reach the Anthropic API. It will try again later.") from None

    if response.stop_reason == "refusal":
        raise GenerationError("Claude declined to write this batch.")
    if response.stop_reason == "max_tokens":
        raise GenerationError("Claude's answer was cut off. Try a smaller batch size.")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text)["sentences"]
    except (ValueError, KeyError, TypeError):
        raise GenerationError("Claude's answer wasn't in the expected format.") from None


# --- Running a batch ---

@dataclass
class Status:
    running: bool = False
    last_started: float | None = None
    last_finished: float | None = None
    last_error: str | None = None
    last_added: int | None = None
    last_skipped: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


status = Status()


@dataclass
class BatchResult:
    batch_id: int
    added: int
    duplicates: int
    invalid: int


def run_batch(db: Session, requested_by: str, generator: Generator | None = None) -> BatchResult:
    """Generate one batch now. Raises GenerationError on failure (nothing is saved)."""
    generator = generator or claude_generator
    prompt = build_prompt(gather_inputs(db))
    items = generator(SYSTEM_PROMPT, prompt)
    if not isinstance(items, list) or not items:
        raise GenerationError("Claude returned no sentences.")

    themes = {t.name.casefold(): t.id for t in db.scalars(select(Theme))}
    batch = GenerationBatch(
        requested_by=requested_by, model=get_config().anthropic_model,
        prompt_snapshot=SYSTEM_PROMPT + "\n\n---\n\n" + prompt,
    )
    db.add(batch)
    db.commit()
    pending = settings_store.get(db, "require_review")
    rows = [
        (str(i.get("arabic", "")), str(i.get("english", "")),
         themes.get(str(i.get("theme", "")).strip().casefold()))
        for i in items if isinstance(i, dict)
    ]
    result = add_sentences(
        db, rows, source=SentenceSource.generated, batch_id=batch.id,
        status=SentenceStatus.pending_review if pending else SentenceStatus.active,
    )
    return BatchResult(batch.id, len(result.added), len(result.duplicates), len(result.invalid))


def run_and_record(session_factory, requested_by: str, generator: Generator | None = None) -> None:
    """Run a batch in its own DB session and record the outcome in `status`. Never raises."""
    if not status.lock.acquire(blocking=False):
        return  # one batch at a time
    status.running = True
    status.last_started = time.time()
    try:
        with session_factory() as db:
            r = run_batch(db, requested_by, generator)
        status.last_error = None
        status.last_added, status.last_skipped = r.added, r.duplicates + r.invalid
    except GenerationError as e:
        status.last_error = str(e)
    except Exception:  # noqa: BLE001 - the game must keep running whatever happens here
        log.exception("Sentence generation failed")
        status.last_error = "Something went wrong while generating. Details are in the server log."
    finally:
        status.last_finished = time.time()
        status.running = False
        status.lock.release()


def unseen_pool_size(db: Session) -> int:
    """How many sentences a brand-new player could still get (active, under the player limit)."""
    done = (
        select(SentenceView.sentence_id, func.count().label("n"))
        .where(SentenceView.outcome.in_([ViewOutcome.answered, ViewOutcome.reported]))
        .group_by(SentenceView.sentence_id).subquery()
    )
    max_players = settings_store.get(db, "max_players")
    return db.scalar(
        select(func.count(Sentence.id))
        .outerjoin(done, done.c.sentence_id == Sentence.id)
        .where(Sentence.status == SentenceStatus.active, func.coalesce(done.c.n, 0) < max_players)
    )


def should_auto_generate(db: Session, now: float | None = None) -> bool:
    now = now or time.time()
    if status.running or not get_config().anthropic_api_key:
        return False
    if status.last_finished is not None:
        cooldown = FAILURE_COOLDOWN if status.last_error else SUCCESS_COOLDOWN
        if now - status.last_finished < cooldown:
            return False
    return unseen_pool_size(db) <= settings_store.get(db, "pool_threshold")
