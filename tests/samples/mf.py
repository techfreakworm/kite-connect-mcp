"""Sample arguments for the write tools of tools/mf.py (see tests/test_invariants.py).

These tools exist only with KITE_MCP_ALLOW_MF_WRITES=true, which the invariant test sets."""

SAMPLE_ARGS = {
    "place_mf_order": {"tradingsymbol": "INF000A01AA0", "transaction_type": "BUY", "amount": 5000},
    "cancel_mf_order": {"order_id": "mf-order-1"},
    "place_mf_sip": {"tradingsymbol": "INF000A01AA0", "amount": 1000, "instalments": 12, "frequency": "monthly"},
    "modify_mf_sip": {"sip_id": "mf-sip-1", "status": "paused"},
    "cancel_mf_sip": {"sip_id": "mf-sip-1"},
}
