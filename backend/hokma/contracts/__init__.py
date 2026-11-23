"""Contract sheets (SPEC-018, H2): one canonical identity per instrument and the multiplier, tick, expiration and strike
that the provider gives. Nothing in Hokma types a multiplier from memory: it comes from a sheet."""
from hokma.core import processguard

processguard.require(__name__)          # runtime limit: only the Hokma server loads this package (core/processguard.py)
