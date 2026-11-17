import { test } from "node:test";
import assert from "node:assert/strict";
import { formatList, formatTodo } from "../src/format.js";

test("format one", () => {
  assert.equal(formatTodo({ id: 1, title: "a", done: true }), "[x] 1. a");
});

test("empty list", () => {
  assert.equal(formatList([]), "(nothing to do)");
});
