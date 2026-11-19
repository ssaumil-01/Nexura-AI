def line_total(item):
    """Price of all units of one item, before tax."""
    return round(item.price * item.qty, 2)


def apply_tax(amount, rate):
    return round(amount * (1 + rate), 2)


def order_total(items, tax_rate):
    return apply_tax(sum(line_total(i) for i in items), tax_rate)
