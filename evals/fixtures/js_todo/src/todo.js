export class TodoList {
  constructor(items = []) {
    this.items = items.map((t) => ({ ...t }));
    this.nextId = this.items.reduce((m, t) => Math.max(m, t.id), 0) + 1;
  }

  add(title) {
    if (!title || !title.trim()) throw new Error("title is required");
    const todo = { id: this.nextId++, title: title.trim(), done: false };
    this.items.push(todo);
    return todo;
  }

  complete(id) {
    const todo = this.items.find((t) => t.id === id);
    if (!todo) throw new Error(`no todo with id ${id}`);
    todo.done = true;
    return todo;
  }

  pending() {
    return this.items.filter((t) => !t.done);
  }

  all() {
    return [...this.items];
  }
}
