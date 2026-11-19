import sys

from inventory.config import load_config
from inventory.reports import low_stock, value_report
from inventory.storage import load_items


def main(argv):
    cfg = load_config()
    items = load_items()
    if argv[:1] == ["report"]:
        print(value_report(items, cfg["currency"]))
        print("LOW STOCK: " + ", ".join(i.sku for i in low_stock(items, cfg["low_stock_threshold"])))
        return 0
    print("usage: python main.py report")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
