"""Account state: holdings, trades and the analysis state attached to them.

Since the account cutover (2026-11-12) the ledger is the only source of real account state;
``mirror`` keeps the column manifest of the retired one-way import from the old account database.
"""
