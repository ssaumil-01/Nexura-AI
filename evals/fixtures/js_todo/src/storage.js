import { existsSync, readFileSync, writeFileSync } from "node:fs";

export const DEFAULT_FILE = "todos.json";

export function load(path = DEFAULT_FILE) {
  return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : [];
}

export function save(items, path = DEFAULT_FILE) {
  writeFileSync(path, JSON.stringify(items, null, 2));
}
