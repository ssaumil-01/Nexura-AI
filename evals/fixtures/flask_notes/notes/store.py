from notes.models import Note


class NoteStore:
    """In-memory note storage with a size limit."""

    def __init__(self, max_notes=100):
        self.max_notes = max_notes
        self._notes = {}
        self._next_id = 1

    def add(self, text):
        if len(self._notes) >= self.max_notes:
            raise ValueError("note limit reached")
        note = Note(id=self._next_id, text=text)
        self._notes[note.id] = note
        self._next_id += 1
        return note

    def get(self, note_id):
        return self._notes.get(note_id)

    def all(self):
        return list(self._notes.values())
