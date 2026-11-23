"""Economic calendar (SPEC-018, H2): macro events, the release catalog and the windows around an event in which a desk does not
open positions."""
from hokma.core import processguard

processguard.require(__name__)          # runtime limit: only the Hokma server loads this package (core/processguard.py)
