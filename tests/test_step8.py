import csv
import io
import json

import pytest

from app import auth, flags, reports, sentences
from app.models import Answer, FlagReview, Player, Sentence


@pytest.fixture
def owner_client(client, db):
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    return client


def _play(client, sid, texts, joins=()):
    pid = client.post("/api/players").json()["player_id"]
    client.get("/api/round", params={"player_id": pid})
    boxes, i = [], 1
    for t in texts:
        end = i + 1 if i in joins else i
        boxes.append({"start": i, "end": end, "text": t})
        i = end + 1
    r = client.post("/api/answers", json={"player_id": pid, "sentence_id": sid, "boxes": boxes})
    assert r.status_code == 200, r.text
    return pid


@pytest.fixture
def data(owner_client, db):
    s = sentences.add_sentences(db, [("هنروح فين", "Where are we going?")]).added[0]
    p1 = _play(owner_client, s.id, ["Hanro7", "Fen"])
    p2 = _play(owner_client, s.id, ["hanro7", "feen"])
    p3 = _play(owner_client, s.id, ["hanro7 fen"], joins=(1,))
    p4 = _play(owner_client, s.id, ["hanroooooo7", "fen"])  # flagged
    db.add(Player(id="p_named0001", display_name="Secret Name"))
    db.commit()
    return s, (p1, p2, p3, p4)


def _csv(r):
    assert r.content.startswith("﻿".encode())
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))


def test_raw_csv(owner_client, db, data):
    s, (p1, p2, p3, p4) = data
    r = owner_client.get("/owner/export/answers.csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and ".csv" in r.headers["content-disposition"]
    rows = _csv(r)
    assert list(rows[0]) == ["sentence_id", "sentence_text", "english", "position_start", "position_end",
                             "arabic", "franco_raw", "franco", "joined", "player_id"]
    assert len(rows) == 6  # 7 boxes; p4's flagged box is left out, its other box stays
    joined = [r for r in rows if r["joined"] == "true"]
    assert joined == [{"sentence_id": str(s.id), "sentence_text": "هنروح فين", "english": "Where are we going?",
                       "position_start": "1", "position_end": "2", "arabic": "هنروح فين",
                       "franco_raw": "hanro7 fen", "franco": "hanro7 fen", "joined": "true", "player_id": p3}]
    assert any(r["franco_raw"] == "Hanro7" and r["franco"] == "hanro7" for r in rows)
    assert "Secret Name" not in r.text and "created_at" not in r.text


def test_raw_options(owner_client, db, data):
    rows = _csv(owner_client.get("/owner/export/answers.csv?flagged=true&timestamps=true"))
    assert len(rows) == 7 and rows[0]["created_at"]
    lines = owner_client.get("/owner/export/answers.jsonl").text.strip().split("\n")
    parsed = [json.loads(x) for x in lines]
    assert len(parsed) == 6 and {p["joined"] for p in parsed} == {True, False}  # real booleans
    assert all(isinstance(p["position_start"], int) for p in parsed)
    assert "هنروح" in owner_client.get("/owner/export/answers.jsonl").text  # not \\u-escaped


def test_grouped(owner_client, db, data):
    rows = _csv(owner_client.get("/owner/export/variants.csv"))
    assert [r["count"] for r in rows] == ["2", "2", "1", "1"]  # most common first
    as_tuples = {(r["arabic"], r["joined"], r["variant"], r["count"]) for r in rows}
    assert as_tuples == {
        ("هنروح", "false", "hanro7", "2"),
        ("فين", "false", "fen", "2"),
        ("فين", "false", "feen", "1"),
        ("هنروح فين", "true", "hanro7 fen", "1"),  # joined stays its own key
    }
    with_flagged = {r["variant"] for r in _csv(owner_client.get("/owner/export/variants.csv?flagged=true"))}
    assert "hanroooooo7" in with_flagged
    lines = [json.loads(x) for x in owner_client.get("/owner/export/variants.jsonl").text.strip().split("\n")]
    assert {"arabic": "هنروح", "joined": False, "variant": "hanro7", "count": 2} in lines


def test_reviews_and_deleted_sentences(owner_client, db, data):
    s, _ = data
    flagged = db.query(Answer).filter_by(flagged=True).one()
    flags.review(db, flagged, FlagReview.kept)
    assert len(_csv(owner_client.get("/owner/export/answers.csv"))) == 7  # kept counts as normal
    flags.review(db, flagged, FlagReview.excluded)
    assert len(_csv(owner_client.get("/owner/export/answers.csv?flagged=true"))) == 6  # excluded never
    reports.delete(db, db.get(Sentence, s.id))
    assert _csv(owner_client.get("/owner/export/answers.csv")) == []


def test_gloss_fix_shows_in_export(owner_client, db, data):
    s, _ = data
    reports.fix(db, db.get(Sentence, s.id), "هنروح فين؟", "Where are we off to?")
    rows = _csv(owner_client.get("/owner/export/answers.csv"))
    assert {r["english"] for r in rows} == {"Where are we off to?"}


def test_export_page_and_access(owner_client, client, db, data):
    page = owner_client.get("/owner/export").text
    assert "6 answers ready to export" in page and "(1 of them)" in page
    assert owner_client.get("/owner/export/answers.xlsx").status_code == 404
    assert owner_client.get("/owner/export/players.csv").status_code == 404
    owner_client.post("/owner/logout")
    r = client.get("/owner/export/answers.csv", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/owner/login"
