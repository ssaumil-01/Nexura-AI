from inventory.pricing import line_total


def low_stock(items, threshold):
    return [i for i in items if i.qty < threshold]


def value_report(items, currency="USD"):
    lines = [f"{i.sku} {i.name}: {line_total(i):.2f}" for i in items]
    total = sum(line_total(i) for i in items)
    lines.append(f"TOTAL: {total:.2f}")
    return "\n".join(lines)
