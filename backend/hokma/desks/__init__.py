"""Money desks (SPEC-014): a desk is a project plus a team of agents, a capital the desk may commit in its broker account and a token budget."""
from hokma.core import processguard

processguard.require(__name__)          # runtime limit: only the Hokma server loads this package (core/processguard.py)
