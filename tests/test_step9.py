from app import flags, sentences
from app.models import Answer, FlagReview, Player


def _player(client):
    return client.post("/api/players").json()["player_id"]


def _answer(client, pid, sid, boxes):
    client.get("/api/round", params={"player_id": pid})
    r = client.post("/api/answers", json={"player_id": pid, "sentence_id": sid, "boxes": boxes})
    assert r.status_code == 200, r.text


def test_me_counts_words_including_joined(client, db):
    s = sentences.add_sentences(db, [("هنروح فين النهارده", "")]).added[0]
    pid = _player(client)
    assert client.get("/api/me", params={"player_id": pid}).json() == {"display_name": None, "words": 0}
    _answer(client, pid, s.id, [{"start": 1, "end": 2, "text": "hanro7 fen"}, {"start": 3, "end": 3, "text": "ennaharda"}])
    assert client.get("/api/me", params={"player_id": pid}).json()["words"] == 3
    assert client.get("/api/me", params={"player_id": "p_00000000"}).status_code == 404


def test_excluded_answers_dont_count(client, db):
    s = sentences.add_sentences(db, [("انا هنا", "")]).added[0]
    pid = _player(client)
    _answer(client, pid, s.id, [{"start": 1, "end": 1, "text": "ana"}, {"start": 2, "end": 2, "text": "qwerty"}])
    flags.review(db, db.query(Answer).filter_by(franco="qwerty").one(), FlagReview.excluded)
    assert client.get("/api/me", params={"player_id": pid}).json()["words"] == 1


def test_set_name(client, db):
    pid = _player(client)
    r = client.post("/api/name", json={"player_id": pid, "display_name": "  Ahmed ​  El  Attar "})
    assert r.json() == {"display_name": "Ahmed El Attar"}
    assert db.get(Player, pid).display_name == "Ahmed El Attar"
    assert client.post("/api/name", json={"player_id": pid, "display_name": "x" * 31}).status_code == 422
    assert client.post("/api/name", json={"player_id": pid, "display_name": "   "}).json() == {"display_name": None}
    assert client.post("/api/name", json={"player_id": "p_00000000", "display_name": "x"}).status_code == 404


def test_leaderboard(client, db):
    s1, s2 = sentences.add_sentences(db, [("جملة اولى خالص", ""), ("جملة تانية", "")]).added
    a, b, anon = _player(client), _player(client), _player(client)
    client.post("/api/name", json={"player_id": a, "display_name": "Sara"})
    client.post("/api/name", json={"player_id": b, "display_name": "Omar"})
    for pid in (a, b, anon):
        got = client.get("/api/round", params={"player_id": pid}).json()["sentence"]
        n = len(got["words"])
        _answer(client, pid, got["id"], [{"start": i, "end": i, "text": f"w{i}"} for i in range(1, n + 1)])
    # Sara does a second sentence too.
    got = client.get("/api/round", params={"player_id": a}).json()["sentence"]
    _answer(client, a, got["id"], [{"start": i, "end": i, "text": f"w{i}"} for i in range(1, len(got["words"]) + 1)])

    r = client.get("/api/leaderboard", params={"player_id": a}).json()
    assert [(p["rank"], p["name"], p["you"]) for p in r["top"]] == [(1, "Sara", True), (2, "Omar", False)]
    assert r["top"][0]["words"] == 5 and r["you"]["rank"] == 1
    # Players without a name aren't listed, and nobody else's ID is revealed.
    assert all("id" not in p for p in r["top"])
    assert client.get("/api/leaderboard", params={"player_id": anon}).json()["you"] is None
    assert client.get("/api/leaderboard").json()["top"][0]["you"] is False


def test_play_page_has_welcome_text(client):
    page = client.get("/").text
    assert "مفيش صح ومفيش غلط" in page and "no right or wrong" in page
    assert 'id="board-open"' in page and 'id="help-open"' in page


def test_owner_can_rename_and_clear_names(client, db):
    from app import auth
    pid = _player(client)
    client.post("/api/name", json={"player_id": pid, "display_name": "Rude Name"})
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    assert 'value="Rude Name"' in client.get("/owner/players").text
    r = client.post(f"/owner/players/{pid}/name", data={"display_name": " Nice   Name "}, follow_redirects=False)
    assert r.headers["location"] == f"/owner/players?renamed={pid}"
    db.expire_all()
    assert db.get(Player, pid).display_name == "Nice Name"
    client.post(f"/owner/players/{pid}/name", data={"display_name": ""})
    db.expire_all()
    assert db.get(Player, pid).display_name is None
    assert client.post(f"/owner/players/{pid}/name", data={"display_name": "x" * 31}).status_code == 422
    assert client.post("/owner/players/p_00000000/name", data={"display_name": "x"}).status_code == 404
    client.post("/owner/logout")
    assert client.post(f"/owner/players/{pid}/name", data={"display_name": "x"}, follow_redirects=False).headers["location"] == "/owner/login"


def test_owner_can_edit_welcome_text(client, db):
    from app import auth, settings_store
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    form = {k: str(v) for k, v in settings_store.DEFAULTS.items() if not isinstance(v, bool)}
    form["welcome_ar"] = "اهلا بيكم\r\n\r\nسطر تاني"
    form["welcome_en"] = "Welcome, friends!"
    assert client.post("/owner/settings", data=form, follow_redirects=False).status_code == 303
    page = client.get("/").text
    assert "<p>اهلا بيكم</p><p>سطر تاني</p>" in page and "<p>Welcome, friends!</p>" in page
    assert "مفيش صح ومفيش غلط" not in page

    form["welcome_en"] = "   "  # empty goes back to the original
    form["welcome_ar"] = "x" * 2001
    r = client.post("/owner/settings", data=form)
    assert r.status_code == 422 and "Keep it under 2000 characters" in r.text
    form["welcome_ar"] = ""
    client.post("/owner/settings", data=form)
    assert "مفيش صح ومفيش غلط" in client.get("/").text and "no right or wrong" in client.get("/").text
    assert "<script>" not in settings_store.get(db, "welcome_en")
