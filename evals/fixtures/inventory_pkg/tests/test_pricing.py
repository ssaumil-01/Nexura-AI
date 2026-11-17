from inventory.models import Item
from inventory.pricing import apply_tax, line_total, order_total


def test_line_total():
    assert line_total(Item("x", "X", 2.5, 4)) == 10.0


def test_apply_tax():
    assert apply_tax(100, 0.2) == 120.0


def test_order_total():
    assert order_total([Item("a", "A", 1.0, 2), Item("b", "B", 3.0, 1)], 0.5) == 7.5
