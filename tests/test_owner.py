import io
from contextlib import nullcontext

import pytest

from app import auth, cli
from app.config import get_config
from app.models import Owner

PASSWORD = "correct horse battery"


@pytest.fixture
def owner(db):
    return auth.create_owner(db, "ahmed", PASSWORD)


def _login(client, username="ahmed", password=PASSWORD):
    return client.post(
        "/owner/login", data={"username": username, "password": password}, follow_redirects=False
    )


def test_panel_requires_login(client):
    for path in ("/owner", "/owner/stats", "/owner/export"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/owner/login"


def test_login_page_renders(client, owner):
    r = client.get("/owner/login")
    assert r.status_code == 200 and "Owner login" in r.text


def test_login_logout_flow(client, owner):
    r = _login(client)
    assert r.status_code == 303 and r.headers["location"] == "/owner"

    r = client.get("/owner")
    assert r.status_code == 200
    assert "ahmed" in r.text and "Reports queue" in r.text and "Export" in r.text

    r = client.get("/owner/login", follow_redirects=False)
    assert r.headers["location"] == "/owner"  # already logged in

    client.post("/owner/logout", follow_redirects=False)
    assert client.get("/owner", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("username,password", [("ahmed", "wrong password!"), ("nobody", PASSWORD)])
def test_bad_login_gives_same_error(client, owner, username, password):
    r = _login(client, username, password)
    assert r.status_code == 401
    assert "Wrong username or password." in r.text
    assert client.get("/owner", follow_redirects=False).status_code == 303


def test_username_is_case_insensitive(client, owner):
    assert _login(client, "  Ahmed ").status_code == 303


def test_section_pages(client, owner):
    _login(client)
    from app.owner import SECTIONS
    for slug in SECTIONS:  # every section is built: none says "coming"
        r = client.get(f"/owner/{slug}")
        assert r.status_code == 200 and "Coming in build step" not in r.text, slug
    assert "All sections" in r.text
    assert "All sections" not in client.get("/owner").text
    assert client.get("/owner/nope").status_code == 404


def test_deleted_owner_is_logged_out(client, db, owner):
    _login(client)
    db.delete(owner)
    db.commit()
    assert client.get("/owner", follow_redirects=False).status_code == 303


def test_password_is_hashed(db, owner):
    assert PASSWORD not in owner.password_hash
    assert owner.password_hash.startswith("$argon2")


@pytest.mark.parametrize(
    "username,password,message",
    [
        ("auto", PASSWORD, "reserved"),
        ("ab", PASSWORD, "3-64 characters"),
        ("has space", PASSWORD, "3-64 characters"),
        ("sara", "short", "at least"),
    ],
)
def test_create_owner_validation(db, username, password, message):
    with pytest.raises(auth.OwnerError, match=message):
        auth.create_owner(db, username, password)


def test_create_owner_duplicate(db, owner):
    with pytest.raises(auth.OwnerError, match="already exists"):
        auth.create_owner(db, "AHMED", PASSWORD)


def test_set_password(client, db, owner):
    auth.set_password(db, "ahmed", "a brand new password")
    assert _login(client).status_code == 401
    assert _login(client, password="a brand new password").status_code == 303


def test_cli(db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    cli.main(["create-owner", "sara", "--password-stdin"])
    assert 'Created owner "sara"' in capsys.readouterr().out
    assert db.query(Owner).filter_by(username="sara").one()

    cli.main(["list-owners"])
    assert capsys.readouterr().out.strip() == "sara"

    with pytest.raises(SystemExit, match="reserved"):
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        cli.main(["create-owner", "auto", "--password-stdin"])


def test_settings_page_shows_values(client, owner):
    _login(client)
    r = client.get("/owner/settings")
    assert r.status_code == 200
    assert "Players per sentence" in r.text and 'value="50"' in r.text


def _form(**overrides):
    from app.settings_store import DEFAULTS

    data = {k: str(v) for k, v in DEFAULTS.items() if not isinstance(v, bool)}
    data.update(overrides)
    return data


def test_settings_save(client, db, owner):
    _login(client)
    r = client.post(
        "/owner/settings",
        data=_form(max_players="30", flag_length_ratio="3.5", require_review="on"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Settings saved." in client.get(r.headers["location"]).text
    from app import settings_store

    values = settings_store.get_all(db)
    assert values["max_players"] == 30
    assert values["flag_length_ratio"] == 3.5
    assert values["require_review"] is True

    # Unchecked box turns it back off.
    client.post("/owner/settings", data=_form())
    assert settings_store.get(db, "require_review") is False


def test_settings_bad_input_saves_nothing(client, db, owner):
    from app import settings_store

    _login(client)
    r = client.post("/owner/settings", data=_form(max_players="30", report_threshold="0", batch_size="abc"))
    assert r.status_code == 422
    assert "Nothing was saved" in r.text
    assert "Must be at least 1" in r.text and "Enter a whole number" in r.text
    assert 'value="abc"' in r.text  # keeps what was typed
    assert settings_store.get(db, "max_players") == 50


def test_settings_requires_login(client):
    assert client.post("/owner/settings", data={}, follow_redirects=False).status_code == 303


# --- First-time setup page ---

SETUP_CODE = "a-long-setup-code"


@pytest.fixture
def setup_code(monkeypatch):
    monkeypatch.setattr(get_config(), "setup_code", SETUP_CODE)


def _setup(client, code=SETUP_CODE, username="ahmed", password=PASSWORD, repeat=PASSWORD):
    return client.post(
        "/owner/setup",
        data={"setup_code": code, "username": username, "password": password, "repeat": repeat},
        follow_redirects=False,
    )


def test_login_sends_to_setup_when_no_owner(client):
    r = client.get("/owner/login", follow_redirects=False)
    assert r.headers["location"] == "/owner/setup"


@pytest.mark.parametrize("code", ["", "too-short"])
def test_setup_off_without_long_enough_code(client, db, monkeypatch, code):
    monkeypatch.setattr(get_config(), "setup_code", code)
    assert "switched off" in client.get("/owner/setup").text
    assert _setup(client, code=code).status_code == 403
    assert db.query(Owner).count() == 0


def test_setup_creates_owner_and_logs_in(client, db, setup_code):
    r = _setup(client, username=" Ahmed ")
    assert r.status_code == 303 and r.headers["location"] == "/owner"
    assert db.query(Owner).one().username == "ahmed"
    assert client.get("/owner").status_code == 200

    # The page is gone for good, even with the right code.
    client.post("/owner/logout")
    assert client.get("/owner/setup", follow_redirects=False).headers["location"] == "/owner/login"
    assert _setup(client, username="intruder").headers["location"] == "/owner/login"
    assert db.query(Owner).count() == 1


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"code": "wrong-setup-code!"}, "The setup code is wrong."),
        ({"repeat": "something else entirely"}, "The passwords don&#39;t match."),
        ({"password": "short", "repeat": "short"}, "at least 10 characters"),
        ({"username": "auto"}, "reserved"),
    ],
)
def test_setup_errors(client, db, setup_code, kwargs, message):
    r = _setup(client, **kwargs)
    assert r.status_code in (403, 422)
    assert message in r.text
    assert db.query(Owner).count() == 0
    assert client.get("/owner", follow_redirects=False).status_code == 303


# --- Change password ---


def _change(client, current=PASSWORD, new="a brand new password", repeat="a brand new password"):
    return client.post(
        "/owner/account",
        data={"current": current, "new": new, "repeat": repeat},
        follow_redirects=False,
    )


def test_account_page(client, owner):
    _login(client)
    r = client.get("/owner/account")
    assert r.status_code == 200 and "Change password" in r.text
    assert 'href="/owner/account"' in client.get("/owner").text  # username links here


def test_change_password(client, owner):
    _login(client)
    r = _change(client)
    assert r.headers["location"] == "/owner/account?changed=1"
    assert "Password changed." in client.get(r.headers["location"]).text
    client.post("/owner/logout")
    assert _login(client).status_code == 401
    assert _login(client, password="a brand new password").status_code == 303


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"current": "not my password"}, "Your current password is wrong."),
        ({"repeat": "a different new password"}, "The new passwords don&#39;t match."),
        ({"new": "short", "repeat": "short"}, "at least 10 characters"),
    ],
)
def test_change_password_errors(client, owner, kwargs, message):
    _login(client)
    r = _change(client, **kwargs)
    assert r.status_code == 422 and message in r.text
    client.post("/owner/logout")
    assert _login(client).status_code == 303  # old password still works


def test_account_requires_login(client):
    assert _change(client).status_code == 303
    assert _change(client).headers["location"] == "/owner/login"
