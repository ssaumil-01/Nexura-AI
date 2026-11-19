def apply_discount(amount, percent):
    """Reduce `amount` by `percent` per cent (0-100)."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return round(amount - percent, 2)
