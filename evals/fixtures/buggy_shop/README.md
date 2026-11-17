# Shop (fixture with a seeded bug)

A shopping cart with percentage discounts.

- Run: `python -c "from shop.cart import Cart; c = Cart(); c.add('apple', 3); print(c.total())"`
- Test: `python -m pytest -q` (baseline: one test fails because of a seeded bug)
