import pytest

from app import auth, sentences, settings_store
from app.models import Answer, Player, Sentence, SentenceStatus, SentenceView, ViewOutcome


def _add(db, text):
    return sentences.add_sentences(db, [(text, "")]).added[0]


def _player(client):
    return client.post("/api/players").json()["player_id"]


def _round(client, pid):
    s = client.get("/api/round", params={"player_id": pid}).json()["sentence"]
    return s and s["id"]


def _answer(client, pid, sid, n_words):
    return client.post("/api/answers", json={"player_id": pid, "sentence_id": sid, "boxes": [
        {"start": i, "end": i, "text": f"w{i}"} for i in range(1, n_words + 1)]})


def test_shown_sentence_comes_back_on_reload(client, db):
    for t in ("جملة اولى", "جملة تانية", "جملة تالتة"):
        _add(db, t)
    pid = _player(client)
    first = _round(client, pid)
    assert all(_round(client, pid) == first for _ in range(5))  # reloading doesn't skip
    view = db.get(SentenceView, (pid, first))
    assert view.outcome is ViewOutcome.shown

    assert _answer(client, pid, first, 2).status_code == 200
    db.expire_all()
    assert db.get(SentenceView, (pid, first)).outcome is ViewOutcome.answered
    assert _round(client, pid) != first


def test_never_shown_again_after_answer_or_report(client, db):
    a, b = _add(db, "جملة اولى"), _add(db, "جملة تانية")
    pid = _player(client)
    db.add(SentenceView(player_id=pid, sentence_id=a.id, outcome=ViewOutcome.reported))
    db.commit()
    assert _round(client, pid) == b.id
    _answer(client, pid, b.id, 2)
    assert _round(client, pid) is None


def test_retired_after_max_players(client, db):
    settings_store.set_value(db, "max_players", 2)
    s = _add(db, "جملة واحدة")
    for _ in range(2):
        pid = _player(client)
        assert _round(client, pid) == s.id
        assert _answer(client, pid, s.id, 2).status_code == 200
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.retired
    assert _round(client, _player(client)) is None


def test_answer_accepted_if_retired_while_typing(client, db):
    settings_store.set_value(db, "max_players", 1)
    s = _add(db, "جملة واحدة")
    slow, fast = _player(client), _player(client)
    assert _round(client, slow) == s.id
    assert _round(client, fast) == s.id
    assert _answer(client, fast, s.id, 2).status_code == 200  # retires it
    assert _answer(client, slow, s.id, 2).status_code == 200  # still saved
    # But someone who was never shown it can't answer a retired sentence.
    assert _answer(client, _player(client), s.id, 2).status_code == 409


def test_double_submit_saves_once(client, db):
    s = _add(db, "جملة واحدة")
    pid = _player(client)
    _round(client, pid)
    assert _answer(client, pid, s.id, 2).status_code == 200
    assert _answer(client, pid, s.id, 2).status_code == 409
    assert db.query(Answer).count() == 2


def test_failed_validation_keeps_sentence_pending(client, db):
    s = _add(db, "جملة واحدة")
    pid = _player(client)
    _round(client, pid)
    r = client.post("/api/answers", json={"player_id": pid, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 2, "text": "عربي"}]})
    assert r.status_code == 422
    assert db.get(SentenceView, (pid, s.id)).outcome is ViewOutcome.shown
    assert _round(client, pid) == s.id


def test_prefers_fewest_players(client, db):
    a, b = _add(db, "جملة اولى"), _add(db, "جملة تانية")
    first = _player(client)
    got = _round(client, first)
    _answer(client, first, got, 2)
    other = b.id if got == a.id else a.id
    assert _round(client, _player(client)) == other


def test_lowering_max_players_retires_full_sentences(client, db):
    s = _add(db, "جملة واحدة")
    for _ in range(3):
        pid = _player(client)
        _round(client, pid)
        _answer(client, pid, s.id, 2)
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    form = {k: str(v) for k, v in settings_store.DEFAULTS.items() if not isinstance(v, bool)}
    form["max_players"] = "3"
    client.post("/owner/settings", data=form)
    db.expire_all()
    assert db.get(Sentence, s.id).status is SentenceStatus.retired


@pytest.fixture
def logged_in(client, db):
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})


def test_players_page_and_delete(client, db, logged_in):
    s = _add(db, "جملة واحدة")
    junk, good = _player(client), _player(client)
    for pid in (junk, good):
        _round(client, pid)
        _answer(client, pid, s.id, 2)
    r = client.get("/owner/players")
    assert junk in r.text and good in r.text

    r = client.get(f"/owner/players/{junk}/delete")
    assert "all 2 of their answers" in r.text
    r = client.post(f"/owner/players/{junk}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/owner/players?deleted={junk}"
    assert f"Deleted {junk}" in client.get(r.headers["location"]).text
    db.expire_all()
    assert db.get(Player, junk) is None
    assert db.query(Answer).filter_by(player_id=junk).count() == 0
    assert db.get(SentenceView, (junk, s.id)) is None
    assert db.query(Answer).filter_by(player_id=good).count() == 2

    # The deleted player's browser just gets a new ID.
    assert client.get("/api/round", params={"player_id": junk}).status_code == 404
    assert client.post(f"/owner/players/{junk}/delete").status_code == 404
