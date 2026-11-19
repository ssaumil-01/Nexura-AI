from flask import Flask, jsonify, request

from notes.store import NoteStore
from settings import load_settings


def create_app(settings=None):
    settings = settings or load_settings()
    app = Flask(__name__)
    store = NoteStore(max_notes=settings["max_notes"])

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/notes")
    def list_notes():
        return jsonify([n.to_dict() for n in store.all()])

    @app.post("/notes")
    def add_note():
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify(error="text is required"), 400
        try:
            note = store.add(text)
        except ValueError as e:
            return jsonify(error=str(e)), 409
        return jsonify(note.to_dict()), 201

    return app


app = create_app()
