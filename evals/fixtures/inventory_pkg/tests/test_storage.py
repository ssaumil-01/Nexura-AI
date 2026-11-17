from inventory.storage import load_items


def test_load_items(tmp_path):
    p = tmp_path / "i.csv"
    p.write_text("sku,name,price,qty\nZ9,Zed,1.25,7\n", encoding="utf-8")
    [item] = load_items(str(p))
    assert (item.sku, item.price, item.qty) == ("Z9", 1.25, 7)
