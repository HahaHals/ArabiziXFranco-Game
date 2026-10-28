import time
from contextlib import nullcontext

import pytest

from app import auth, generation, sentences, settings_store
from app.config import get_config
from app.generation import GenerationError
from app.models import GenerationBatch, Sentence, SentenceSource, SentenceStatus, TargetWord, Theme

GOOD = [
    {"arabic": "انا جعان جدا النهارده", "english": "I'm really hungry today", "theme": "Ordering food"},
    {"arabic": "الأوردر وصل بارد خالص", "english": "The order arrived completely cold", "theme": "delivery complaints"},
    {"arabic": "انا جعان جدا النهارده!", "english": "duplicate of the first", "theme": "ordering food"},
    {"arabic": "I am English", "english": "not Arabic", "theme": "ordering food"},
    {"arabic": "عايز اروح البنك بكرة", "english": "I want to go to the bank tomorrow", "theme": "unknown theme"},
]


class FakeClaude:
    def __init__(self, items=GOOD, error=None):
        self.items, self.error, self.calls = items, error, []

    def __call__(self, system, prompt):
        self.calls.append((system, prompt))
        if self.error:
            raise GenerationError(self.error)
        return self.items


@pytest.fixture(autouse=True)
def reset_status():
    generation.status = generation.Status()
    yield
    generation.status = generation.Status()


@pytest.fixture
def themes(db):
    for name in ("ordering food", "delivery complaints"):
        db.add(Theme(name=name, description="", active=True))
    db.add(Theme(name="banking", description="", active=False))
    db.commit()


def test_batch_adds_sentences_with_theme_and_batch(db, themes):
    r = generation.run_batch(db, "ahmed", FakeClaude())
    assert (r.added, r.duplicates, r.invalid) == (3, 1, 1)
    batch = db.get(GenerationBatch, r.batch_id)
    assert batch.requested_by == "ahmed" and batch.model == get_config().anthropic_model
    assert "Egyptian colloquial Arabic" in batch.prompt_snapshot
    rows = db.query(Sentence).order_by(Sentence.id).all()
    assert all(s.source is SentenceSource.generated and s.batch_id == batch.id for s in rows)
    assert all(s.status is SentenceStatus.active for s in rows)
    theme_names = [s.theme.name if s.theme else None for s in rows]
    assert theme_names == ["ordering food", "delivery complaints", None]


def test_require_review_makes_batch_pending(db, themes):
    settings_store.set_value(db, "require_review", True)
    generation.run_batch(db, "auto", FakeClaude())
    assert {s.status for s in db.query(Sentence)} == {SentenceStatus.pending_review}


def test_prompt_contents(db, themes):
    sentences.add_sentences(db, [("الأكل اتأخر ساعة", "")])
    db.add(TargetWord(word="معلش", active=True))
    db.add(TargetWord(word="خلاص", active=False))
    db.commit()
    settings_store.set_value(db, "batch_size", 20)
    fake = FakeClaude()
    generation.run_batch(db, "auto", fake)
    system, prompt = fake.calls[0]
    assert "Never Modern Standard Arabic" in system
    assert "Write 20 new sentences" in prompt
    assert "- ordering food" in prompt and "- delivery complaints" in prompt and "banking" not in prompt
    assert "معلش" in prompt and "خلاص" not in prompt  # inactive target word left out
    assert "اتأخر" in prompt  # under-covered word from the pool
    assert "- الأكل اتأخر ساعة" in prompt  # existing sentence, don't repeat


def test_no_themes_falls_back_to_daily_life(db):
    fake = FakeClaude()
    generation.run_batch(db, "auto", fake)
    assert "- daily life" in fake.calls[0][1]


def test_target_word_drops_out_when_enough_answers(db, client):
    settings_store.set_value(db, "target_word_answer_threshold", 1)
    db.add(TargetWord(word="انا", active=True))
    db.commit()
    s = sentences.add_sentences(db, [("انا هنا", "")]).added[0]
    pid = client.post("/api/players").json()["player_id"]
    client.get("/api/round", params={"player_id": pid})
    client.post("/api/answers", json={"player_id": pid, "sentence_id": s.id, "boxes": [
        {"start": 1, "end": 1, "text": "ana"}, {"start": 2, "end": 2, "text": "hena"}]})
    assert generation.gather_inputs(db).target_words == []


def test_failure_saves_nothing_and_is_recorded(db):
    generation.run_and_record(lambda: nullcontext(db), "auto", FakeClaude(error="credit balance is too low"))
    assert generation.status.last_error == "credit balance is too low"
    assert db.query(Sentence).count() == 0 and db.query(GenerationBatch).count() == 0
    assert not generation.status.running


def test_empty_answer_is_an_error(db):
    with pytest.raises(GenerationError):
        generation.run_batch(db, "auto", FakeClaude(items=[]))


def test_missing_api_key_message(monkeypatch):
    monkeypatch.setattr(get_config(), "anthropic_api_key", "")
    with pytest.raises(GenerationError, match="No Anthropic API key"):
        generation.claude_generator("s", "p")


# --- automatic trigger ---

def test_auto_trigger_rules(db, monkeypatch):
    monkeypatch.setattr(get_config(), "anthropic_api_key", "sk-test")
    settings_store.set_value(db, "pool_threshold", 2)
    sentences.add_sentences(db, [("جملة اولى", ""), ("جملة تانية", ""), ("جملة تالتة", "")])
    assert generation.unseen_pool_size(db) == 3
    assert not generation.should_auto_generate(db)  # 3 > 2
    settings_store.set_value(db, "pool_threshold", 3)
    assert generation.should_auto_generate(db)  # 3 <= 3

    now = time.time()
    generation.status.last_finished, generation.status.last_error = now - 60, "boom"
    assert not generation.should_auto_generate(db, now)  # failure cooldown
    assert generation.should_auto_generate(db, now + generation.FAILURE_COOLDOWN)
    generation.status.last_error = None
    assert not generation.should_auto_generate(db, now)  # success cooldown
    generation.status.running = True
    assert not generation.should_auto_generate(db, now + 3600)

    generation.status = generation.Status()
    monkeypatch.setattr(get_config(), "anthropic_api_key", "")
    assert not generation.should_auto_generate(db)  # no key, no calls


def test_round_triggers_background_batch(client, db, monkeypatch):
    monkeypatch.setattr(get_config(), "anthropic_api_key", "sk-test")
    calls = []
    monkeypatch.setattr(generation, "run_and_record", lambda factory, who, gen=None: calls.append(who))
    pid = client.post("/api/players").json()["player_id"]
    r = client.get("/api/round", params={"player_id": pid})
    assert r.json() == {"sentence": None}  # the player isn't kept waiting
    assert calls == ["auto"]


def test_game_works_when_generation_fails(client, db, monkeypatch):
    sentences.add_sentences(db, [("جملة اولى", "")])
    monkeypatch.setattr(get_config(), "anthropic_api_key", "sk-test")
    monkeypatch.setattr(generation, "claude_generator", FakeClaude(error="Anthropic API error (529)"))
    from app import db as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: nullcontext(db))
    pid = client.post("/api/players").json()["player_id"]
    r = client.get("/api/round", params={"player_id": pid})
    assert r.status_code == 200 and r.json()["sentence"]["text"] == "جملة اولى"
    assert generation.status.last_error == "Anthropic API error (529)"


# --- owner pages ---

@pytest.fixture
def owner_client(client, db):
    auth.create_owner(db, "ahmed", "correct horse battery")
    client.post("/owner/login", data={"username": "ahmed", "password": "correct horse battery"})
    return client


def test_themes_page(owner_client, db):
    r = owner_client.post("/owner/themes", data={"name": " ordering   food ", "description": "Delivery apps"}, follow_redirects=False)
    assert r.headers["location"] == "/owner/themes?done=added"
    t = db.query(Theme).one()
    assert (t.name, t.description, t.active) == ("ordering food", "Delivery apps", True)
    r = owner_client.post("/owner/themes", data={"name": "Ordering Food"})
    assert r.status_code == 422 and "already a theme" in r.text
    assert owner_client.post("/owner/themes", data={"name": ""}).status_code == 422
    owner_client.post("/owner/themes", data={"theme_id": t.id, "name": "food", "description": ""})
    owner_client.post(f"/owner/themes/{t.id}/toggle")
    db.expire_all()
    assert (db.get(Theme, t.id).name, db.get(Theme, t.id).active) == ("food", False)
    assert "food" in owner_client.get("/owner/themes").text


def test_target_words_page(owner_client, db):
    r = owner_client.post("/owner/target-words", data={"words": "معلش خلاص\nmerci معلش"}, follow_redirects=False)
    assert r.headers["location"] == "/owner/target-words?added=2&skipped=2"
    assert sorted(w.word for w in db.query(TargetWord)) == ["خلاص", "معلش"]
    page = owner_client.get("/owner/target-words").text
    assert "0 / 50" in page
    w = db.query(TargetWord).filter_by(word="خلاص").one()
    owner_client.post(f"/owner/target-words/{w.id}/remove")
    assert [w.word for w in db.query(TargetWord)] == ["معلش"]


def test_generation_page_and_manual_run(owner_client, db, monkeypatch):
    monkeypatch.setattr(get_config(), "anthropic_api_key", "")
    page = owner_client.get("/owner/generation").text
    assert "No Anthropic API key is set" in page

    monkeypatch.setattr(get_config(), "anthropic_api_key", "sk-test")
    monkeypatch.setattr(generation, "claude_generator", FakeClaude())
    from app import db as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: nullcontext(db))
    r = owner_client.post("/owner/generation/run", follow_redirects=False)
    assert r.headers["location"] == "/owner/generation?done=started"
    assert db.query(GenerationBatch).one().requested_by == "ahmed"
    page = owner_client.get("/owner/generation").text
    assert "Last batch: 3 added, 2 skipped" in page


def test_review_queue(owner_client, db):
    owner_client.post("/owner/generation/review-setting", data={"require_review": "on"})
    assert settings_store.get(db, "require_review") is True
    r = generation.run_batch(db, "auto", FakeClaude())
    page = owner_client.get("/owner/generation").text
    assert f"Batch {r.batch_id}" in page and "Approve all 3" in page

    a, b, c = db.query(Sentence).order_by(Sentence.id).all()
    owner_client.post(f"/owner/generation/{a.id}/approve")
    owner_client.post(f"/owner/generation/{b.id}/delete")
    bad = owner_client.post(f"/owner/generation/{c.id}/edit", data={"arabic": a.text, "english": "x"})
    assert bad.status_code == 422 and "already in the game" in bad.text
    owner_client.post(f"/owner/generation/{c.id}/edit", data={"arabic": "عايز اروح البنك بعد بكرة", "english": "day after"})
    db.expire_all()
    assert db.get(Sentence, a.id).status is SentenceStatus.active
    assert db.get(Sentence, b.id).status is SentenceStatus.deleted
    c = db.get(Sentence, c.id)
    assert c.status is SentenceStatus.pending_review and c.words[-2:] == ["بعد", "بكرة"]
    owner_client.post(f"/owner/generation/batch/{r.batch_id}/approve")
    db.expire_all()
    assert db.get(Sentence, c.id).status is SentenceStatus.active
    assert owner_client.post(f"/owner/generation/{c.id}/approve").status_code == 404  # no longer pending

    owner_client.post("/owner/generation/review-setting", data={})
    assert settings_store.get(db, "require_review") is False


def test_pending_sentences_not_served(client, db):
    settings_store.set_value(db, "require_review", True)
    generation.run_batch(db, "auto", FakeClaude())
    pid = client.post("/api/players").json()["player_id"]
    assert client.get("/api/round", params={"player_id": pid}).json() == {"sentence": None}


# --- the real SDK call, against a pretend Anthropic server ---

def _sse(events):
    import json as _json
    return "".join(f"event: {e['type']}\ndata: {_json.dumps(e)}\n\n" for e in events)


def _fake_anthropic(text, stop_reason="end_turn", status=200, seen=None):
    import json as _json
    import anthropic
    import httpx2 as httpx

    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}})
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
             "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 50}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(events))

    return anthropic.Anthropic(api_key="sk-test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_real_sdk_request_and_parsing(monkeypatch):
    import json as _json
    monkeypatch.setattr(get_config(), "anthropic_api_key", "sk-test")
    seen = []
    client = _fake_anthropic(_json.dumps({"sentences": GOOD[:2]}), seen=seen)
    assert generation.claude_generator("SYSTEM", "PROMPT", client) == GOOD[:2]
    body = _json.loads(seen[0].content)
    assert body["model"] == get_config().anthropic_model
    assert body["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in body
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "low"
    assert body["system"] == "SYSTEM" and body["messages"][0]["content"] == "PROMPT"
    assert body["stream"] is True


@pytest.mark.parametrize("kwargs,message", [
    ({"text": "not json"}, "expected format"),
    ({"text": "", "stop_reason": "refusal"}, "declined"),
    ({"text": "{\"sente", "stop_reason": "max_tokens"}, "cut off"),
    ({"text": "", "status": 400}, "credit balance is too low"),
])
def test_real_sdk_failures(monkeypatch, kwargs, message):
    monkeypatch.setattr(get_config(), "anthropic_api_key", "sk-test")
    with pytest.raises(GenerationError, match=message):
        generation.claude_generator("s", "p", _fake_anthropic(**kwargs))
