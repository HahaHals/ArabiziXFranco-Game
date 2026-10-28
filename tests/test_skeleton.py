import pytest
from sqlalchemy.exc import IntegrityError

from app import settings_store
from app.models import (
    Answer,
    Player,
    Report,
    ReportReason,
    Sentence,
    SentenceSource,
    SentenceStatus,
)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def _sentence(db, text="أنا رايح الشغل"):
    s = Sentence(
        text=text,
        normalized_text=text,
        words=text.split(),
        english="I'm going to work",
        source=SentenceSource.owner,
    )
    db.add(s)
    db.commit()
    return s


def test_sentence_defaults_and_arabic_roundtrip(db):
    s = _sentence(db)
    db.expire_all()
    s = db.get(Sentence, s.id)
    assert s.status is SentenceStatus.active
    assert s.words == ["أنا", "رايح", "الشغل"]
    assert s.created_at is not None


def test_answer_row(db):
    s = _sentence(db)
    db.add(Player(id="p_test"))
    db.add(
        Answer(
            sentence_id=s.id, player_id="p_test", position_start=1, position_end=1,
            arabic="أنا", franco_raw="Ana", franco="ana",
        )
    )
    db.commit()
    a = db.query(Answer).one()
    assert (a.joined, a.flagged, a.flag_reason) == (False, False, None)


def test_player_can_report_sentence_only_once(db):
    s = _sentence(db)
    db.add(Player(id="p_test"))
    db.add(Report(sentence_id=s.id, player_id="p_test", reason=ReportReason.misspelled))
    db.commit()
    db.add(Report(sentence_id=s.id, player_id="p_test", reason=ReportReason.inappropriate))
    with pytest.raises(IntegrityError):
        db.commit()


def test_foreign_keys_enforced(db):
    db.add(Report(sentence_id=999, player_id="nobody", reason=ReportReason.misspelled))
    with pytest.raises(IntegrityError):
        db.commit()


def test_settings_defaults_and_overrides(db):
    assert settings_store.get(db, "max_players") == 50
    assert settings_store.get(db, "require_review") is False
    settings_store.set_value(db, "max_players", 10)
    settings_store.set_value(db, "require_review", True)
    settings_store.set_value(db, "flag_length_ratio", 3)
    values = settings_store.get_all(db)
    assert values["max_players"] == 10
    assert values["require_review"] is True
    assert values["flag_length_ratio"] == 3.0
    assert values["report_threshold"] == 2  # untouched default


def test_settings_reject_bad_input(db):
    with pytest.raises(settings_store.UnknownSetting):
        settings_store.set_value(db, "nope", 1)
    with pytest.raises(TypeError):
        settings_store.set_value(db, "max_players", "10")
    with pytest.raises(TypeError):
        settings_store.set_value(db, "max_players", True)
    with pytest.raises(TypeError):
        settings_store.set_value(db, "require_review", 1)


def test_duplicate_sentence_rejected_even_if_deleted(db):
    s = _sentence(db)
    s.status = SentenceStatus.deleted
    db.commit()
    with pytest.raises(IntegrityError):
        _sentence(db)


def test_settings_range_checked(db):
    with pytest.raises(ValueError, match="at most 1000"):
        settings_store.set_value(db, "max_players", 5000)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_settings_reject_non_finite_numbers(db, raw):
    values, errors = settings_store.parse_form({"flag_length_ratio": raw})
    assert errors["flag_length_ratio"] == "Enter a number"
