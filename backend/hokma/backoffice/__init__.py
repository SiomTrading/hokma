"""Back office (SPEC-018, H2): the book of every fill, the average cost, the snapshot of an account, the profit and loss, the
reconciliation against the broker statement and the reports. Every number is computed here, never by a model."""
from hokma.core import processguard

processguard.require(__name__)          # runtime limit: only the Hokma server loads this package (core/processguard.py)
