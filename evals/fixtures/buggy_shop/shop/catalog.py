PRICES = {"apple": 0.5, "bread": 2.25, "cheese": 6.0, "coffee": 9.5}


def price_of(product):
    if product not in PRICES:
        raise KeyError(f"unknown product: {product}")
    return PRICES[product]
