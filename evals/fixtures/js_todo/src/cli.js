import { TodoList } from "./todo.js";
import { formatList, formatTodo } from "./format.js";
import { load, save } from "./storage.js";

const [command, ...args] = process.argv.slice(2);
const list = new TodoList(load());

if (command === "add") {
  const todo = list.add(args.join(" "));
  save(list.all());
  console.log(`added ${formatTodo(todo)}`);
} else if (command === "done") {
  list.complete(Number(args[0]));
  save(list.all());
  console.log("ok");
} else if (command === "list") {
  console.log(formatList(list.all()));
} else {
  console.log("usage: node src/cli.js add <title> | done <id> | list");
  process.exitCode = 1;
}
