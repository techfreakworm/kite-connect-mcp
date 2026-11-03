"""Sample arguments for the write tools of tools/alerts.py (see tests/test_invariants.py)."""

ALERT = {
    "name": "NIFTY 50 above 27000",
    "type": "simple",
    "lhs_exchange": "INDICES",
    "lhs_tradingsymbol": "NIFTY 50",
    "operator": ">=",
    "rhs_type": "constant",
    "rhs_constant": 27000,
}
UUID = "aaaaaaaa-1111-4111-8111-bbbbbbbbbbbb"

SAMPLE_ARGS = {
    "create_alert": ALERT,
    "modify_alert": {"uuid": UUID, **ALERT},
    "delete_alerts": {"uuids": [UUID]},
}
