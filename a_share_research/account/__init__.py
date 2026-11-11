"""Account state: holdings, trades and the analysis state attached to them.

Until the account cutover the tables hold a one-way mirror of the old account database
(see ``mirror``); the old runtime stays the only writer of real account state.
"""
