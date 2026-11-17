from inventory.models import Item
from inventory.reports import low_stock, value_report


def test_low_stock():
    items = [Item("a", "A", 1.0, 2), Item("b", "B", 1.0, 9)]
    assert [i.sku for i in low_stock(items, 5)] == ["a"]


def test_value_report_total():
    assert value_report([Item("a", "A", 1.5, 2)]).splitlines()[-1] == "TOTAL: 3.00"
