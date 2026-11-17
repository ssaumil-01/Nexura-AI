import pytest

from shop.cart import Cart


def test_subtotal():
    c = Cart()
    c.add("apple", 4)
    c.add("bread")
    assert c.subtotal() == 4.25


def test_total_without_discount():
    c = Cart()
    c.add("cheese")
    assert c.total() == 6.0


def test_total_with_discount():
    c = Cart()
    c.add("coffee", 2)
    c.set_discount(10)
    assert c.total() == 17.1


def test_unknown_product():
    with pytest.raises(KeyError):
        Cart().add("caviar")
