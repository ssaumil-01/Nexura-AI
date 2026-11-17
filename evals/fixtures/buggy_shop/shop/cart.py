from shop.catalog import price_of
from shop.discounts import apply_discount


class Cart:
    def __init__(self):
        self.lines = {}
        self.discount_percent = 0

    def add(self, product, qty=1):
        if qty < 1:
            raise ValueError("qty must be at least 1")
        price_of(product)  # validates the product
        self.lines[product] = self.lines.get(product, 0) + qty

    def subtotal(self):
        return round(sum(price_of(p) * q for p, q in self.lines.items()), 2)

    def set_discount(self, percent):
        self.discount_percent = percent

    def total(self):
        return apply_discount(self.subtotal(), self.discount_percent)
