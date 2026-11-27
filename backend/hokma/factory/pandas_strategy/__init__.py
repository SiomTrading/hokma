"""Strategies written in pandas (1.3.0, task 3433): `PARAMS` and `generate_signals(df, params)`.

contract.py   the format: the parameters, the table of signals and what each one must hold
sessions.py   the columns that Hokma gives the table: session_id and is_rth
adapter.py    the strategy of the factory that replays the signals bar by bar in the backtest and the optimizer
loader.py     reads the format and loads and runs the code in this process, in a module of its own, as any Python runs
checks.py     the dry run: it runs, it decides, it is deterministic and it does not look at the future
"""
