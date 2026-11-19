export function formatTodo(todo) {
  return `${todo.done ? "[x]" : "[ ]"} ${todo.id}. ${todo.title}`;
}

export function formatList(todos) {
  return todos.length ? todos.map(formatTodo).join("\n") : "(nothing to do)";
}
