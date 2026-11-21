"""Tests never probe optional native parts in child processes (task 3288); the probe tests turn it back on."""
import os

from hokma.core import processguard

os.environ.setdefault("HOKMA_NATIVE_PROBE", "off")
# Tests run the sensitive packages in the pytest process, as the server does. Set the module variable directly: the
# public entry points refuse any caller that is not one of Hokma's. Child processes start without it (test_processguard).
processguard._role = processguard.SERVER
