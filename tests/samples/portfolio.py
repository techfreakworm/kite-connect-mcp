"""Sample arguments for the write tools of tools/portfolio.py (see tests/test_invariants.py)."""

SAMPLE_ARGS = {
    "convert_position": {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "position_type": "overnight",
        "quantity": 3,
        "old_product": "NRML",
        "new_product": "MIS",
    },
    "authorise_holdings": {},  # every argument is optional: no holdings means the entire holdings
}
