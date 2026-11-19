"""TEST-ONLY correct solutions for evals/tasks_v2.yaml (used by evals/oracle.py). Each solution is a list of tool calls built by
applying exact edits to the fixture files, so a fixture change that breaks a solution fails loudly here."""
from pathlib import Path

FIX = Path(__file__).with_name("fixtures")


def _src(fixture: str, path: str) -> str:
    return (FIX / fixture / path).read_text(encoding="utf-8")


def _edit(fixture: str, path: str, *pairs: tuple[str, str]) -> list[tuple[str, dict]]:
    """Read the file (read-before-write is enforced), then write it back with each `old` replaced once by `new`."""
    s = _src(fixture, path)
    for old, new in pairs:
        assert s.count(old) == 1, f"oracle_v2: {fixture}/{path}: {old[:40]!r} found {s.count(old)} times"
        s = s.replace(old, new, 1)
    return [("read_file", {"target_file": path}), ("write_file", {"target_file": path, "content": s})]


def _rename(fixture: str, paths: list[str], old: str, new: str) -> list[tuple[str, dict]]:
    out = []
    for p in paths:
        s = _src(fixture, p)
        assert old in s, (p, old)
        out += [("read_file", {"target_file": p}), ("write_file", {"target_file": p, "content": s.replace(old, new)})]
    return out


def _new(path: str, content: str) -> list[tuple[str, dict]]:
    return [("create_file", {"target_file": path, "content": content})]


RETURN_APP = "    return app\n\n\napp = create_app()"

SOLUTIONS_V2: dict[str, list[tuple[str, dict]]] = {
    # ── flask_notes ──
    "fn_delete": _edit("flask_notes", "notes/store.py", ("    def all(self):", "    def delete(self, note_id):\n"
                                                         "        return self._notes.pop(note_id, None) is not None\n\n    def all(self):"))
    + _edit("flask_notes", "app.py", (RETURN_APP, '    @app.delete("/notes/<int:note_id>")\n    def delete_note(note_id):\n'
                                      '        if not store.delete(note_id):\n            return jsonify(error="note not found"), 404\n'
                                      '        return "", 204\n\n' + RETURN_APP)),
    "fn_search": _edit("flask_notes", "notes/store.py", ("    def all(self):", "    def search(self, text):\n"
                                                         "        q = text.lower()\n"
                                                         "        return [n for n in self._notes.values() if q in n.text.lower()]\n\n"
                                                         "    def all(self):"))
    + _edit("flask_notes", "app.py", (RETURN_APP, '    @app.get("/notes/search")\n    def search_notes():\n'
                                      '        q = request.args.get("q", "").strip()\n        if not q:\n'
                                      '            return jsonify(error="q is required"), 400\n'
                                      '        return jsonify([n.to_dict() for n in store.search(q)])\n\n' + RETURN_APP)),
    "fn_count": _edit("flask_notes", "app.py", (RETURN_APP, '    @app.get("/notes/count")\n    def count_notes():\n'
                                                '        return jsonify(count=len(store.all()))\n\n' + RETURN_APP)),
    "fn_max_length": _edit("flask_notes", "settings.json", ('"max_notes": 100', '"max_notes": 100,\n  "max_length": 200'))
    + _edit("flask_notes", "settings.py", ('DEFAULTS = {"title": "Notes", "max_notes": 100}',
                                           'DEFAULTS = {"title": "Notes", "max_notes": 100, "max_length": 200}'))
    + _edit("flask_notes", "app.py",
            ('    store = NoteStore(max_notes=settings["max_notes"])',
             '    store = NoteStore(max_notes=settings["max_notes"])\n    max_length = settings.get("max_length", 200)'),
            ('            return jsonify(error="text is required"), 400',
             '            return jsonify(error="text is required"), 400\n        if len(text) > max_length:\n'
             '            return jsonify(error=f"text is longer than {max_length} characters"), 400')),
    "fn_title_header": _edit("flask_notes", "app.py", (RETURN_APP, '    @app.after_request\n    def add_title(response):\n'
                                                       '        response.headers["X-App-Title"] = settings.get("title", "Notes")\n'
                                                       '        return response\n\n' + RETURN_APP)),
    # ── js_todo ──
    "jt_remove": _edit("js_todo", "src/todo.js", ("  pending() {", "  remove(id) {\n    const i = this.items.findIndex((t) => t.id === id);\n"
                                                  "    if (i < 0) throw new Error(`no todo with id ${id}`);\n"
                                                  "    return this.items.splice(i, 1)[0];\n  }\n\n  pending() {"))
    + _edit("js_todo", "src/cli.js", ('} else if (command === "list") {', '} else if (command === "remove") {\n'
                                      '  list.remove(Number(args[0]));\n  save(list.all());\n  console.log("ok");\n'
                                      '} else if (command === "list") {')),
    "jt_rename_format": _rename("js_todo", ["src/format.js", "src/cli.js", "test/format.test.js"], "formatTodo", "renderTodo"),
    "jt_due_dates": _edit("js_todo", "src/todo.js",
                          ("  add(title) {", "  add(title, due) {"),
                          ('    const todo = { id: this.nextId++, title: title.trim(), done: false };',
                           '    if (due !== undefined && !/^\\d{4}-\\d{2}-\\d{2}$/.test(due)) throw new Error("due must be YYYY-MM-DD");\n'
                           '    const todo = { id: this.nextId++, title: title.trim(), done: false, ...(due ? { due } : {}) };'))
    + _edit("js_todo", "src/format.js", ('  return `${todo.done ? "[x]" : "[ ]"} ${todo.id}. ${todo.title}`;',
                                         '  const due = todo.due ? ` (due ${todo.due})` : "";\n'
                                         '  return `${todo.done ? "[x]" : "[ ]"} ${todo.id}. ${todo.title}${due}`;')),
    "jt_todo_file": _edit("js_todo", "src/storage.js",
                          ("export function load(path = DEFAULT_FILE) {", "const file = () => process.env.TODO_FILE || DEFAULT_FILE;\n\n"
                                                                          "export function load(path = file()) {"),
                          ("export function save(items, path = DEFAULT_FILE) {", "export function save(items, path = file()) {")),
    # ── inventory_pkg ──
    "inv_rename": _rename("inventory_pkg", ["inventory/pricing.py", "inventory/reports.py", "tests/test_pricing.py"],
                          "line_total", "item_total"),
    "inv_bulk_discount": _edit("inventory_pkg", "inventory.json", ('"currency": "EUR"', '"currency": "EUR",\n  "bulk_min_qty": 10,\n'
                                                                    '  "bulk_discount_percent": 5'))
    + _edit("inventory_pkg", "inventory/config.py", ('DEFAULTS = {"tax_rate": 0.0, "low_stock_threshold": 5, "currency": "USD"}',
                                                     'DEFAULTS = {"tax_rate": 0.0, "low_stock_threshold": 5, "currency": "USD", '
                                                     '"bulk_min_qty": 10, "bulk_discount_percent": 5}'))
    + _edit("inventory_pkg", "inventory/pricing.py",
            ('def line_total(item):\n    """Price of all units of one item, before tax."""\n    return round(item.price * item.qty, 2)',
             'def line_total(item, cfg=None):\n    """Price of all units of one item, before tax (bulk discount when cfg is given)."""\n'
             '    total = item.price * item.qty\n    if cfg and item.qty >= cfg["bulk_min_qty"]:\n'
             '        total *= 1 - cfg["bulk_discount_percent"] / 100\n    return round(total, 2)'))
    + _edit("inventory_pkg", "inventory/reports.py",
            ('def value_report(items, currency="USD"):\n    lines = [f"{i.sku} {i.name}: {line_total(i):.2f}" for i in items]\n'
             '    total = sum(line_total(i) for i in items)',
             'def value_report(items, currency="USD", cfg=None):\n    lines = [f"{i.sku} {i.name}: {line_total(i, cfg):.2f}" for i in items]\n'
             '    total = sum(line_total(i, cfg) for i in items)'))
    + _edit("inventory_pkg", "main.py", ('print(value_report(items, cfg["currency"]))', 'print(value_report(items, cfg["currency"], cfg))')),
    "inv_export_csv": _edit("inventory_pkg", "inventory/reports.py",
                            ("from inventory.pricing import line_total\n", "import csv\n\nfrom inventory.pricing import line_total\n"),
                            ("def value_report(", 'def export_low_stock_csv(items, threshold, path):\n'
                                                  '    with open(path, "w", newline="", encoding="utf-8") as f:\n'
                                                  '        w = csv.writer(f)\n        w.writerow(["sku", "name", "qty"])\n'
                                                  '        for i in low_stock(items, threshold):\n'
                                                  '            w.writerow([i.sku, i.name, i.qty])\n\n\ndef value_report('))
    + _edit("inventory_pkg", "main.py",
            ("from inventory.reports import low_stock, value_report", "from inventory.reports import export_low_stock_csv, low_stock, value_report"),
            ('    print("usage: python main.py report")',
             '    if argv[:1] == ["export-low-stock"] and len(argv) == 2:\n'
             '        export_low_stock_csv(items, cfg["low_stock_threshold"], argv[1])\n        print(f"wrote {argv[1]}")\n'
             '        return 0\n    print("usage: python main.py report | export-low-stock <path>")')),
    "inv_currency": _edit("inventory_pkg", "inventory/reports.py",
                          ('def value_report(items, currency="USD"):\n    lines = [f"{i.sku} {i.name}: {line_total(i):.2f}" for i in items]',
                           'def value_report(items, currency=None):\n    unit = f" {currency}" if currency else ""\n'
                           '    lines = [f"{i.sku} {i.name}: {line_total(i):.2f}{unit}" for i in items]'),
                          ('    lines.append(f"TOTAL: {total:.2f}")', '    lines.append(f"TOTAL: {total:.2f}{unit}")')),
    "inv_validation": _edit("inventory_pkg", "inventory/storage.py",
                            ('    with open(path, newline="", encoding="utf-8") as f:\n'
                             '        return [Item(row["sku"], row["name"], float(row["price"]), int(row["qty"])) for row in csv.DictReader(f)]',
                             '    items = []\n    with open(path, newline="", encoding="utf-8") as f:\n'
                             '        for line_no, row in enumerate(csv.DictReader(f), start=2):\n'
                             '            item = Item(row["sku"], row["name"], float(row["price"]), int(row["qty"]))\n'
                             '            if item.price < 0 or item.qty < 0:\n'
                             '                raise ValueError(f"line {line_no}: price and qty must not be negative")\n'
                             '            items.append(item)\n    return items')),
    # ── buggy_shop ──
    "bs_fix_bug": _edit("buggy_shop", "shop/discounts.py", ("    return round(amount - percent, 2)",
                                                           "    return round(amount * (1 - percent / 100), 2)")),
    "bs_grand_total": _edit("buggy_shop", "shop/cart.py", ("        return apply_discount(self.subtotal(), self.discount_percent)\n",
                                                           "        return apply_discount(self.subtotal(), self.discount_percent)\n\n"
                                                           "    def shipping(self):\n        return 4.99 if self.total() < 50 else 0\n\n"
                                                           "    def grand_total(self):\n"
                                                           "        return round(self.total() + self.shipping(), 2)\n")),
    "bs_coupons": _new("shop/coupons.py", 'COUPONS = {"SAVE10": 10, "HALF": 50}\n')
    + _edit("buggy_shop", "shop/cart.py",
            ("from shop.catalog import price_of\n", "from shop.catalog import price_of\nfrom shop.coupons import COUPONS\n"),
            ("    def total(self):", "    def apply_coupon(self, code):\n        percent = COUPONS.get(code.upper())\n"
                                    "        if percent is None:\n            raise ValueError(f\"unknown coupon: {code}\")\n"
                                    "        self.set_discount(percent)\n\n    def total(self):")),
    "bs_remove": _edit("buggy_shop", "shop/cart.py", ("    def subtotal(self):", "    def remove(self, product, qty=None):\n"
                                                      "        if product not in self.lines:\n            raise KeyError(product)\n"
                                                      "        if qty is None or qty >= self.lines[product]:\n"
                                                      "            del self.lines[product]\n        else:\n"
                                                      "            self.lines[product] -= qty\n\n    def subtotal(self):")),
}
