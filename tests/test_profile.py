"""User display names: captured at sign-up, shown by /auth/me, editable."""

from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401


def register(client, email, **extra):
    return client.post(
        "/auth/register", json={"email": email, "password": PASSWORD, **extra}
    )


def me(client, headers):
    return client.get("/auth/me", headers=headers).json()


def test_name_is_stored_and_returned(client):
    assert register(client, "k@example.com", name="Krishna Panwala").json()["name"] == "Krishna Panwala"
    t = client.post("/auth/login", json={"email": "k@example.com", "password": PASSWORD})
    h = {"Authorization": f"Bearer {t.json()['access_token']}"}
    assert me(client, h)["name"] == "Krishna Panwala"


def test_name_whitespace_is_collapsed(client):
    r = register(client, "w@example.com", name="  Rahul    Patel  ")
    assert r.json()["name"] == "Rahul Patel"


def test_name_is_optional_for_api_clients_and_null_in_me(client):
    h = signup(client, "legacy@example.com")
    assert me(client, h)["name"] is None


def test_blank_or_huge_or_control_name_rejected(client):
    assert register(client, "a@example.com", name="   ").status_code == 422
    assert register(client, "b@example.com", name="x" * 101).status_code == 422
    assert register(client, "c@example.com", name="bad\x00name").status_code == 422
    assert register(client, "d@example.com", name="y" * 201).status_code == 422


def test_rejected_name_does_not_create_the_account(client):
    register(client, "e@example.com", name="   ")
    assert client.post("/auth/login", json={"email": "e@example.com", "password": PASSWORD}).status_code == 401


def test_patch_me_sets_and_changes_name(client):
    h = signup(client, "p@example.com")
    r = client.patch("/auth/me", json={"name": "Priya  Shah"}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Priya Shah"
    assert me(client, h)["name"] == "Priya Shah"
    client.patch("/auth/me", json={"name": "Priya S"}, headers=h)
    assert me(client, h)["name"] == "Priya S"


def test_patch_me_validation_and_auth(client):
    h = signup(client, "q@example.com")
    assert client.patch("/auth/me", json={"name": " "}, headers=h).status_code == 422
    assert client.patch("/auth/me", json={}, headers=h).status_code == 422
    assert client.patch("/auth/me", json={"name": "Z"}).status_code == 401


def test_patch_me_only_changes_own_name(client):
    h1 = signup(client, "one@example.com")
    h2 = signup(client, "two@example.com")
    client.patch("/auth/me", json={"name": "One"}, headers=h1)
    assert me(client, h2)["name"] is None
