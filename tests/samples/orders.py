"""Sample arguments for the write tools of tools/orders.py (see tests/test_invariants.py)."""

SAMPLE_ARGS = {
    "place_order": {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 1,
        "product": "CNC",
        "order_type": "LIMIT",
        "price": 1500.0,
    },
    "modify_order": {"order_id": "111", "price": 1490.0},
    "cancel_order": {"order_id": "111"},
}
