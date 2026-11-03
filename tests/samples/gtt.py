"""Sample arguments for the write tools of tools/gtt.py (see tests/test_invariants.py)."""

GTT = {
    "type": "single",
    "exchange": "NSE",
    "tradingsymbol": "INFY",
    "trigger_values": [1400.0],
    "last_price": 1500.0,
    "orders": [{"transaction_type": "BUY", "quantity": 1, "price": 1400.5, "product": "CNC"}],
}

SAMPLE_ARGS = {
    "place_gtt_order": GTT,
    "modify_gtt_order": {"trigger_id": 123, **GTT},
    "delete_gtt_order": {"trigger_id": 123},
}
