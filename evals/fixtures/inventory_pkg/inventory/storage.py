import csv

from inventory.models import Item


def load_items(path="data/items.csv"):
    with open(path, newline="", encoding="utf-8") as f:
        return [Item(row["sku"], row["name"], float(row["price"]), int(row["qty"])) for row in csv.DictReader(f)]
