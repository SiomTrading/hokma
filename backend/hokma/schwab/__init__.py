"""Schwab Trader API, read only (SPEC-015). Every call goes through `http.py`, the only file that writes the host."""
from hokma.core import processguard

processguard.require(__name__)          # runtime limit: only the Hokma server loads this package (core/processguard.py)
