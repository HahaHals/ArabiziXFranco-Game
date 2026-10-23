import pytest

from app import auth, sentences
from app.models import Answer, Sentence, SentenceStatus
from app.text import clean, franco_error, normalize_sentence, split_words


# --- text ---

def test_split_words_strips_punctuation():
    assert split_words("«انا» رايح، الشغل؟!") == ["انا", "رايح", "الشغل"]
    assert split_words("كده،وبعدين") == ["كده", "وبعدين"]


def test_normalize_sentence_ignores_spacing_punctuation_and_harakat():
    assert normalize_sentence("انا  رايح الشغل؟") == normalize_sentence("انا رايح الشغل")
    assert normalize_sentence("أنَا رايِح") == normalize_sentence("أنا رايح")


def test_clean():
    assert clean("  Ma   3rafsh ") == "ma 3rafsh"
    assert clean("ma3rafsh") != clean("ma 3rafsh")
    assert clean("3’ali") == "3'ali"


@pytest.mark.parametrize("raw,expected", [
    ("ana", None), ("Ma 3rafsh", None), ("2ahwa", None), ("ma-3rafsh", None), ("3’ali", None),
    ("   ", "Fill in this box"),
    ("أنا", "Write it in Franco, not Arabic"),
    ("ana أنا", "Write it in Franco, not Arabic"),
    ("😂😂", "Use English letters or numbers"),
    ("ana!", "Only English letters, numbers, ' and - are allowed"),
    ("a" * 101, "Too long (max 100 characters)"),
])
def test_franco_error(raw, expected):
    assert franco_error(raw, "كلمة") == expected


@pytest.mark.parametrize("raw,arabic,expected", [
    ("72", "كلمة", None), ("45", "كلمة", None), ("123", "كلمة", None), ("7", "كلمة", None),
    ("55", "كلمة", "Repeated numbers aren't a spelling"),
    ("555", "كلمة", "Repeated numbers aren't a spelling"),
    ("22222", "كلمة", "Repeated numbers aren't a spelling"),
    ("3 3", "كلمة", "Repeated numbers aren't a spelling"),
    ("55", "٥٥", None),  # the Arabic is a number
    ("555a", "كلمة", None),
])
def test_numbers_only(raw, arabic, expected):
    assert franco_error(raw, arabic) == expected


# --- adding sentences ---

def test_parse_bulk():
    assert sentences.parse_bulk("انا رايح | I'm going\n\nهنروح فين\tWhere to\nكده") == [
        ("انا رايح", "I'm going"), ("هنروح فين", "Where to"), ("كده", ""),
    ]


def test_add_sentences_skips_duplicates(db):
    sentences.add_sentences(db, [("انا رايح الشغل", "")])
    r = sentences.add_sentences(db, [
        ("انا رايح الشغل؟", ""),   # same as existing
        ("هنروح فين", "Where to"),
        ("هنروح  فين!", ""),       # same as one earlier in this paste
        ("؟؟", ""),                # no words
    ])
    assert [s.text for s in r.added] == ["هنروح فين"]
    assert r.duplicates == ["انا رايح الشغل؟", "هنروح فين!"]
    assert r.invalid == [("؟؟", "No words")]
    s = db.query(Sentence).filter_by(text="هنروح فين").one()
    assert s.words == ["هنروح", "فين"] and s.english == "Where to" and s.status is SentenceStatus.active


# --- player API ---

@pytest.fixture
def player(client):
    return client.post("/api/players").json()["player_id"]


def _add(db, text="انا مش عارف", english="I don't know"):
    return sentences.add_sentences(db, [(text, english)]).added[0]


def test_new_player(client, player):
    assert player.startswith("p_") and len(player) == 10


def test_round_unknown_player(client):
    assert client.get("/api/round", params={"player_id": "p_00000000"}).status_code == 404
    assert client.get("/api/round", params={"player_id": "nonsense"}).status_code == 404


def test_round_and_submit(client, db, player):
    s = _add(db)
    r = client.get("/api/round", params={"player_id": player}).json()
    assert r["sentence"] == {"id": s.id, "text": "انا مش عارف", "english": "I don't know",
                             "words": ["انا", "مش", "عارف"]}

    r = client.post("/api/answers", json={"player_id": player, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": " Ana "},
        {"start": 2, "end": 3, "text": "Msh  3aref"},
    ]})
    assert r.status_code == 200 and r.json() == {"saved": 2}
    rows = db.query(Answer).order_by(Answer.position_start).all()
    assert [(a.position_start, a.position_end, a.arabic, a.franco_raw, a.franco, a.joined) for a in rows] == [
        (1, 1, "انا", " Ana ", "ana", False),
        (2, 3, "مش عارف", "Msh  3aref", "msh 3aref", True),
    ]

    # Answered: not offered again, and can't be submitted twice.
    assert client.get("/api/round", params={"player_id": player}).json() == {"sentence": None}
    r = client.post("/api/answers", json={"player_id": player, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 3, "text": "x"}]})
    assert r.status_code == 409


def test_submit_rejects_bad_boxes_and_saves_nothing(client, db, player):
    s = _add(db)
    r = client.post("/api/answers", json={"player_id": player, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "أنا"},
        {"start": 2, "end": 2, "text": "  "},
        {"start": 3, "end": 3, "text": "3aref"},
    ]})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == {"1": "Write it in Franco, not Arabic", "2": "Fill in this box"}
    assert db.query(Answer).count() == 0


@pytest.mark.parametrize("boxes", [
    [{"start": 1, "end": 1, "text": "a"}, {"start": 2, "end": 2, "text": "b"}],                  # word 3 missing
    [{"start": 1, "end": 2, "text": "a"}, {"start": 2, "end": 3, "text": "b"}],                  # overlap
    [{"start": 1, "end": 4, "text": "a"}],                                                      # past the end
    [{"start": 2, "end": 3, "text": "a"}],                                                      # word 1 missing
])
def test_submit_rejects_boxes_not_matching_words(client, db, player, boxes):
    s = _add(db)
    r = client.post("/api/answers", json={"player_id": player, "sentence_id": s.id, "boxes": boxes})
    assert r.status_code == 422 and db.query(Answer).count() == 0


def test_round_prefers_fewer_answers(client, db, player):
    a = _add(db, "الجملة الأولى")
    b = _add(db, "الجملة التانية")
    other = client.post("/api/players").json()["player_id"]
    client.post("/api/answers", json={"player_id": other, "sentence_id": a.id, "boxes": [
        {"start": 1, "end": 2, "text": "el gomla el ola"}]})
    assert client.get("/api/round", params={"player_id": player}).json()["sentence"]["id"] == b.id


def test_play_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "play.js" in r.text


# --- owner pages ---

@pytest.fixture
def logged_in(client, db):
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})


def test_owner_add_one_and_bulk(client, db, logged_in):
    r = client.post("/owner/sentences", data={"arabic": "انا رايح الشغل", "english": "I'm going to work"})
    assert "Added 1 sentence." in r.text and "انا رايح الشغل" in r.text
    r = client.post("/owner/sentences", data={"bulk": "انا رايح الشغل\nهنروح فين | Where to\nكده تمام"})
    assert "Added 2 sentences." in r.text and "Skipped 1 already in the game" in r.text
    assert db.query(Sentence).count() == 3


def test_owner_empty_submit(client, logged_in):
    r = client.post("/owner/sentences", data={})
    assert r.status_code == 422 and "Type or paste a sentence first" in r.text


def test_stats_page(client, db, logged_in, player):
    s = _add(db)
    client.post("/api/answers", json={"player_id": player, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "ana"}, {"start": 2, "end": 2, "text": "msh"},
        {"start": 3, "end": 3, "text": "3aref"}]})
    r = client.get("/owner/stats")
    assert r.status_code == 200 and "Most covered words" in r.text and "عارف" in r.text


def test_rejects_non_arabic_sentences(db):
    r = sentences.add_sentences(db, [
        ("I'm going to work", ""),
        ("انا رايح work", ""),
        ("عندي ٣ قطط", ""),  # Arabic digits are fine
    ])
    assert r.invalid == [("I'm going to work", "Not Arabic"),
                         ("انا رايح work", 'Not Arabic ("work")')]
    assert [s.text for s in r.added] == ["عندي ٣ قطط"]
