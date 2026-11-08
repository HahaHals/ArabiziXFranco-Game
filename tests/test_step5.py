import pytest

from app import auth, sentences, settings_store
from app.models import Answer, Report, Sentence, SentenceStatus, SentenceView, ViewOutcome


def _add(db, text, english=""):
    return sentences.add_sentences(db, [(text, english)]).added[0]


def _player(client):
    return client.post("/api/players").json()["player_id"]


def _round(client, pid):
    s = client.get("/api/round", params={"player_id": pid}).json()["sentence"]
    return s and s["id"]


def _report(client, pid, sid, reason="misspelled", comment=None):
    return client.post("/api/reports", json={
        "player_id": pid, "sentence_id": sid, "reason": reason, "comment": comment})


def _answer(client, pid, sid, n):
    return client.post("/api/answers", json={"player_id": pid, "sentence_id": sid, "boxes": [
        {"start": i, "end": i, "text": f"w{i}"} for i in range(1, n + 1)]})


def _reported_by(client, db, sid, n):
    for _ in range(n):
        pid = _player(client)
        assert _round(client, pid) == sid
        assert _report(client, pid, sid).status_code == 200


# --- players reporting ---

def test_report_skips_sentence_and_saves_nothing(client, db):
    a, b = _add(db, "جملة اولى"), _add(db, "جملة تانية")
    pid = _player(client)
    first = _round(client, pid)
    r = _report(client, pid, first, "not_egyptian", "  ده فصحى  ")
    assert r.status_code == 200
    rep = db.query(Report).one()
    assert (rep.reason.value, rep.comment, rep.resolved) == ("not_egyptian", "ده فصحى", False)
    assert db.get(SentenceView, (pid, first)).outcome is ViewOutcome.reported
    assert db.query(Answer).count() == 0
    assert _round(client, pid) == (b.id if first == a.id else a.id)


def test_report_once_and_only_current_sentence(client, db):
    s = _add(db, "جملة واحدة")
    pid = _player(client)
    assert _report(client, pid, s.id).status_code == 409  # never shown it
    _round(client, pid)
    assert _report(client, pid, s.id).status_code == 200
    assert _report(client, pid, s.id).status_code == 409  # already reported
    assert _answer(client, pid, s.id, 2).status_code == 409  # can't answer after reporting


def test_bad_reason_rejected(client, db):
    s = _add(db, "جملة واحدة")
    pid = _player(client)
    _round(client, pid)
    assert _report(client, pid, s.id, "boring").status_code == 422


def test_pulled_at_threshold(client, db):
    s = _add(db, "جملة واحدة")
    _reported_by(client, db, s.id, 1)
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.active
    _reported_by(client, db, s.id, 1)
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.reported
    assert _round(client, _player(client)) is None


# --- owner queue ---

@pytest.fixture
def owner_client(client, db):
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    return client


def _pulled(client, db, text="انا رايح الشغل", english="I go work"):
    s = _add(db, text, english)
    _reported_by(client, db, s.id, 2)
    db.expire_all()
    return db.get(Sentence, s.id)


def test_queue_lists_reasons_and_comments(owner_client, db):
    s = _pulled(owner_client, db)
    live = _add(db, "جملة تانية")
    pid = _player(owner_client)
    _round(owner_client, pid)
    _report(owner_client, pid, live.id, "inappropriate", "مش كويسة")
    r = owner_client.get("/owner/reports")
    assert "Taken out of the game <small class=\"muted\">(1)" in r.text
    assert "Reported but still in the game <small class=\"muted\">(1)" in r.text
    assert s.text in r.text and "Misspelled" in r.text and "مش كويسة" in r.text


def test_dismiss_puts_back_and_old_reports_stop_counting(owner_client, db):
    s = _pulled(owner_client, db)
    r = owner_client.post(f"/owner/sentences/{s.id}/dismiss", follow_redirects=False)
    assert r.headers["location"] == "/owner/reports?done=dismissed"
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.active
    assert db.query(Report).filter_by(resolved=False).count() == 0
    _reported_by(owner_client, db, s.id, 1)  # one new report isn't enough again
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.active


def test_fix_gloss_only_keeps_row_and_answers(owner_client, db):
    s = _add(db, "انا رايح الشغل", "I go work")
    pid = _player(owner_client)
    _round(owner_client, pid)
    _answer(owner_client, pid, s.id, 3)
    _reported_by(owner_client, db, s.id, 2)
    r = owner_client.post(f"/owner/sentences/{s.id}/fix",
                          data={"arabic": "انا رايح الشغل!", "english": "I'm going to work"},
                          follow_redirects=False)
    assert r.headers["location"] == "/owner/reports?done=fixed"
    db.expire_all()
    s = db.get(Sentence, s.id)
    assert (s.status, s.english, s.text) == (SentenceStatus.active, "I'm going to work", "انا رايح الشغل!")
    assert db.query(Sentence).count() == 1
    assert db.query(Answer).filter_by(sentence_id=s.id).count() == 3


def test_fix_new_words_makes_new_sentence(owner_client, db):
    s = _pulled(owner_client, db, "انا رايح الشغلل")
    r = owner_client.post(f"/owner/sentences/{s.id}/fix",
                          data={"arabic": "انا رايح الشغل", "english": "I'm going to work"},
                          follow_redirects=False)
    assert r.headers["location"] == "/owner/reports?done=replaced"
    db.expire_all()
    old = db.get(Sentence, s.id)
    new = db.query(Sentence).filter_by(replaces_id=old.id).one()
    assert old.status is SentenceStatus.retired and old.text == "انا رايح الشغلل"
    assert new.status is SentenceStatus.active and new.words == ["انا", "رايح", "الشغل"]
    assert "Taken out of the game <small class=\"muted\">(0)" in owner_client.get("/owner/reports").text


@pytest.mark.parametrize("arabic,message", [
    ("", "The Arabic can&#39;t be empty"),
    ("I go to work", "Not Arabic"),
    ("جملة موجودة", "That sentence is already in the game"),
])
def test_fix_errors(owner_client, db, arabic, message):
    s = _pulled(owner_client, db)
    _add(db, "جملة موجودة")
    r = owner_client.post(f"/owner/sentences/{s.id}/fix", data={"arabic": arabic, "english": "x"})
    assert r.status_code == 422 and message in r.text
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.reported


def test_delete_and_restore(owner_client, db):
    s = _pulled(owner_client, db)
    owner_client.post(f"/owner/sentences/{s.id}/delete")
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.deleted
    r = owner_client.get("/owner/deleted")
    assert s.text in r.text
    # Deleted text still blocks re-adding it.
    assert sentences.add_sentences(db, [(s.text, "")]).duplicates == [s.text]
    r = owner_client.post(f"/owner/sentences/{s.id}/restore", follow_redirects=False)
    assert r.headers["location"] == "/owner/deleted?restored=1"
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.active
    assert owner_client.post(f"/owner/sentences/{s.id}/restore").status_code == 409


def test_lowering_threshold_pulls(owner_client, db):
    settings_store.set_value(db, "report_threshold", 5)
    s = _add(db, "جملة واحدة")
    _reported_by(owner_client, db, s.id, 1)
    form = {k: str(v) for k, v in settings_store.DEFAULTS.items() if not isinstance(v, bool)}
    form["report_threshold"] = "1"
    owner_client.post("/owner/settings", data=form)
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.reported


def test_owner_actions_require_login(client, db):
    s = _add(db, "جملة واحدة")
    for action in ("fix", "dismiss", "delete", "restore"):
        r = client.post(f"/owner/sentences/{s.id}/{action}", follow_redirects=False)
        assert r.headers["location"] == "/owner/login"
