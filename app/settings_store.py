"""Owner-tunable game settings, stored in the `settings` table.

Keys that were never written fall back to their default, so a fresh database works
without seeding. Values are JSON-encoded and checked against the spec's type and range.
"""

import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting


@dataclass(frozen=True)
class Spec:
    default: bool | int | float | str
    label: str
    help: str
    group: str
    step: int  # build step that starts using it
    min: float | None = None
    max: float | None = None  # for text: the maximum length


SPECS: dict[str, Spec] = {
    "max_players": Spec(
        50, "Players per sentence",
        "A sentence is retired once this many different players have done it.",
        "Rotation", 4, 1, 1000,
    ),
    "report_threshold": Spec(
        2, "Reports to pull a sentence",
        "A sentence is taken out of the game once it gets this many reports.",
        "Reports", 5, 1, 50,
    ),
    "require_review": Spec(
        False, "Review generated sentences first",
        "If on, sentences from Claude wait for an owner to approve them. "
        "If off, they go live right away.",
        "Generation", 6,
    ),
    "batch_size": Spec(
        50, "Sentences per batch", "How many sentences Claude writes each time.",
        "Generation", 6, 1, 100,
    ),
    "pool_threshold": Spec(
        10, "Generate more at",
        "Claude writes a new batch when a typical player has this many unseen sentences left, or fewer.",
        "Generation", 6, 0, 500,
    ),
    "target_word_answer_threshold": Spec(
        50, "Answers per target word",
        "Claude keeps using a target word until it has this many answers.",
        "Generation", 6, 1, 10000,
    ),
    "flag_length_ratio": Spec(
        4.0, "Flag long answers: times the Arabic length",
        "An answer is flagged if it's longer than this many times the Arabic word's length, "
        "plus the extra characters below.",
        "Junk filter", 7, 1, 20,
    ),
    "flag_length_slack": Spec(
        4, "Flag long answers: extra characters", "Added to the limit above.",
        "Junk filter", 7, 0, 50,
    ),
    "flag_repeat_run": Spec(
        5, "Flag repeated letters",
        'An answer is flagged if one character repeats this many times in a row (e.g. "aaaaa").',
        "Junk filter", 7, 2, 20,
    ),
    "rate_limit_per_minute": Spec(
        20, "Max submissions per minute", "Per player. A network address (shared by friends on the same Wi-Fi) gets 5 times this.",
        "Rate limiting", 7, 1, 600,
    ),
    # Welcome screen (3.1). One paragraph per line.
    "welcome_ar": Spec(
        "اكتب كل كلمة بالفرانكو زي ما بتكتبها عادي. مفيش صح ومفيش غلط.\n"
        "لو لقيت جملة فيها غلطة إملائية، أو مش مصري، أو مش لايقة، أو مش مفهومة، دوس على «إبلاغ».\n"
        "اتأكد إن الأوتوكوريكت أو الكتابة التلقائية في الكيبورد مش بتغيّر اللي بتكتبه.\n"
        "إجاباتك بتتحفظ من غير اسمك، وبنستخدمها عشان نبني داتا.",
        "Arabic text", "Shown first. Each line is a paragraph.", "Welcome screen", 9, None, 2000,
    ),
    "welcome_en": Spec(
        "Write each word the way you'd naturally type it in Franco. There's no right or wrong.\n"
        "If a sentence looks misspelled, isn't Egyptian, is inappropriate, or doesn't make sense, tap Report.\n"
        "Make sure your keyboard's autocorrect or autocomplete isn't changing what you type.\n"
        "Your answers are saved anonymously and used to build a dataset.",
        "English text", "Shown under the Arabic. Each line is a paragraph.", "Welcome screen", 9, None, 2000,
    ),
}

DEFAULTS: dict[str, Any] = {key: spec.default for key, spec in SPECS.items()}


class UnknownSetting(KeyError):
    pass


def _check(key: str, value: Any) -> Any:
    if key not in SPECS:
        raise UnknownSetting(key)
    spec = SPECS[key]
    expected = type(spec.default)
    if expected is str:
        if not isinstance(value, str):
            raise TypeError(f"{key} must be text")
        value = value.replace("\r\n", "\n").strip()
        if spec.max is not None and len(value) > spec.max:
            raise ValueError(f"Keep it under {spec.max:g} characters")
        return value or spec.default  # empty goes back to the original text
    # bool is a subclass of int; keep them strictly apart.
    if expected is bool and not isinstance(value, bool):
        raise TypeError(f"{key} must be a bool")
    if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{key} must be an int")
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{key} must be a number")
        value = float(value)
        if not math.isfinite(value):  # float() accepts "nan" and "inf"
            raise ValueError("Enter a number")
    if spec.min is not None and value < spec.min:
        raise ValueError(f"Must be at least {spec.min:g}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"Must be at most {spec.max:g}")
    return value


def get_all(db: Session) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in db.scalars(select(Setting)):
        if row.key in DEFAULTS:
            values[row.key] = json.loads(row.value)
    return values


def get(db: Session, key: str) -> Any:
    if key not in DEFAULTS:
        raise UnknownSetting(key)
    row = db.get(Setting, key)
    return DEFAULTS[key] if row is None else json.loads(row.value)


def set_many(db: Session, values: dict[str, Any]) -> None:
    """Check every value first, then save them all in one commit (all or nothing)."""
    checked = {key: _check(key, value) for key, value in values.items()}
    for key, value in checked.items():
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)
    db.commit()


def set_value(db: Session, key: str, value: Any) -> None:
    set_many(db, {key: value})


def parse_form(form: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Turn submitted form fields into typed values. Returns (values, errors by key)."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, spec in SPECS.items():
        if isinstance(spec.default, bool):
            values[key] = key in form  # unchecked boxes aren't submitted
            continue
        if isinstance(spec.default, str):
            try:
                values[key] = _check(key, form.get(key, ""))
            except ValueError as e:
                values[key] = form.get(key, "")
                errors[key] = str(e)
            continue
        raw = form.get(key, "").strip()
        try:
            value = int(raw) if isinstance(spec.default, int) else float(raw)
        except ValueError:
            values[key] = raw
            errors[key] = "Enter a whole number" if isinstance(spec.default, int) else "Enter a number"
            continue
        try:
            values[key] = _check(key, value)
        except ValueError as e:
            values[key] = value
            errors[key] = str(e)
    return values, errors
