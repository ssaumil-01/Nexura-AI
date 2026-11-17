import { test } from "node:test";
import assert from "node:assert/strict";
import { TodoList } from "../src/todo.js";

test("add and complete", () => {
  const l = new TodoList();
  const t = l.add("write tests");
  l.complete(t.id);
  assert.equal(l.pending().length, 0);
});

test("empty title is rejected", () => {
  assert.throws(() => new TodoList().add("  "));
});

test("ids continue after loading", () => {
  const l = new TodoList([{ id: 4, title: "old", done: false }]);
  assert.equal(l.add("new").id, 5);
});
