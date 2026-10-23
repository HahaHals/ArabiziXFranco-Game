import pytest

from app import auth, flags, ratelimit, sentences, settings_store
from app.models import Answer, FlagReview
from app.text import flag_reason


@pytest.mark.parametrize("raw,arabic,expected", [
    ("fen", "فين", None),
    ("ya3niiii", "يعني", None),            # stretched, 4 in a row: real typing
    ("ma 3rafsh", "معرفش", None),
    ("hanro7 fen", "هنروح فين", None),
    ("kteeeeer", "كتير", 'Repeated character ("eeeee")'),
    ("aaaaaaa", "آه", 'Repeated character ("aaaaaaa")'),
    ("AAAAA", "آه", 'Repeated character ("aaaaa")'),
    ("qwert", "قال", "Looks like keyboard mashing"),
    ("lkjhg", "قال", "Looks like keyboard mashing"),
    ("abcdefghijklmnopqrst", "فين", "Much longer than the Arabic (20 vs 3 letters)"),
])
def test_flag_reason(raw, arabic, expected):
    assert flag_reason(raw, arabic, 4.0, 4, 5) == expected


def test_flag_thresholds_are_settings():
    assert flag_reason("ya3niiii", "يعني", 4.0, 4, 4) == 'Repeated character ("iiii")'
    assert flag_reason("abcdefghij", "فين", 2.0, 0, 5) is not None


def _setup(client, db):
    s = sentences.add_sentences(db, [("انا كتير", "")]).added[0]
    pid = client.post("/api/players").json()["player_id"]
    client.get("/api/round", params={"player_id": pid})
    return s, pid


def test_flagged_answers_are_saved_with_reason(client, db):
    s, pid = _setup(client, db)
    r = client.post("/api/answers", json={"player_id": pid, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "ana"}, {"start": 2, "end": 2, "text": "kteeeeeer"}]})
    assert r.status_code == 200
    a, b = db.query(Answer).order_by(Answer.position_start).all()
    assert (a.flagged, a.flag_reason) == (False, None)
    assert b.flagged and b.flag_reason == 'Repeated character ("eeeeee")'


def test_recheck_respects_reviews_and_new_thresholds(client, db):
    s, pid = _setup(client, db)
    client.post("/api/answers", json={"player_id": pid, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "anaaaaa"}, {"start": 2, "end": 2, "text": "kteeeeer"}]})
    a, b = db.query(Answer).order_by(Answer.position_start).all()
    flags.review(db, a, FlagReview.kept)
    assert (a.flagged, a.flag_review) == (False, FlagReview.kept)
    settings_store.set_value(db, "flag_repeat_run", 10)
    assert flags.recheck_all(db) == 0
    db.expire_all()
    assert not db.get(Answer, b.id).flagged  # no longer suspicious under the new threshold
    settings_store.set_value(db, "flag_repeat_run", 5)
    assert flags.recheck_all(db) == 1
    db.expire_all()
    assert db.get(Answer, b.id).flagged and not db.get(Answer, a.id).flagged  # kept stays kept


@pytest.fixture
def owner_client(client, db):
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    return client


def test_flagged_page_keep_and_exclude(owner_client, db):
    s, pid = _setup(owner_client, db)
    owner_client.post("/api/answers", json={"player_id": pid, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "qwerty"}, {"start": 2, "end": 2, "text": "kteeeeer"}]})
    a, b = db.query(Answer).order_by(Answer.position_start).all()
    page = owner_client.get("/owner/flagged").text
    assert "qwerty" in page and "kteeeeer" in page and "keyboard mashing" in page

    r = owner_client.post(f"/owner/flagged/{a.id}/exclude", follow_redirects=False)
    assert r.headers["location"] == "/owner/flagged?done=exclude"
    owner_client.post(f"/owner/flagged/{b.id}/keep")
    db.expire_all()
    assert db.get(Answer, a.id).flag_review is FlagReview.excluded
    assert db.get(Answer, b.id).flag_review is FlagReview.kept and not db.get(Answer, b.id).flagged
    page = owner_client.get("/owner/flagged").text
    assert "Nothing flagged right now" in page and "1 kept, 1 excluded" in page
    assert owner_client.post(f"/owner/flagged/{a.id}/maybe").status_code == 404


def test_recheck_button_and_settings_save(owner_client, db):
    s, pid = _setup(owner_client, db)
    owner_client.post("/api/answers", json={"player_id": pid, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "ana"}, {"start": 2, "end": 2, "text": "kteeer"}]})
    form = {k: str(v) for k, v in settings_store.DEFAULTS.items() if not isinstance(v, bool)}
    form["flag_repeat_run"] = "3"
    owner_client.post("/owner/settings", data=form)
    assert db.query(Answer).filter_by(flagged=True).count() == 1
    r = owner_client.post("/owner/flagged/recheck", follow_redirects=False)
    assert r.headers["location"] == "/owner/flagged?rechecked=1"


# --- rate limiting ---

def test_rate_limit_per_player(client, db):
    settings_store.set_value(db, "rate_limit_per_minute", 3)
    s, pid = _setup(client, db)
    bad = {"player_id": pid, "sentence_id": s.id, "boxes": [{"start": 1, "end": 2, "text": "  "}]}
    codes = [client.post("/api/answers", json=bad).status_code for _ in range(4)]
    assert codes == [422, 422, 422, 429]
    assert "Too many submissions" in client.post("/api/answers", json=bad).json()["detail"]


def test_rate_limit_per_ip_is_higher(client, db):
    settings_store.set_value(db, "rate_limit_per_minute", 2)
    codes = [client.post("/api/players").status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]  # 2 x 5 per IP


def test_rate_limit_window_slides():
    from types import SimpleNamespace
    req = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))
    for t in (0, 1):
        ratelimit.check(req, "p_x", 2, now=t)
    with pytest.raises(Exception):
        ratelimit.check(req, "p_x", 2, now=30)
    ratelimit.check(req, "p_x", 2, now=61)  # the first hit has expired
    ratelimit.check(req, "p_other", 2, now=61)  # another player on the same IP is fine
