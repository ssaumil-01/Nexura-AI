from app import create_app


def client(**overrides):
    return create_app({"title": "T", "max_notes": 100, **overrides}).test_client()


def test_health():
    assert client().get("/health").get_json() == {"status": "ok"}


def test_add_and_list():
    c = client()
    assert c.post("/notes", json={"text": "hello"}).status_code == 201
    assert [n["text"] for n in c.get("/notes").get_json()] == ["hello"]


def test_empty_text_is_rejected():
    assert client().post("/notes", json={"text": "  "}).status_code == 400


def test_note_limit():
    c = client(max_notes=1)
    c.post("/notes", json={"text": "a"})
    assert c.post("/notes", json={"text": "b"}).status_code == 409
