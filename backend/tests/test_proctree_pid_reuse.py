"""A pid remembered by the tree is not killed when the number now belongs to another process (task 3175)."""
import os
import subprocess
import sys
import time

import pytest

from hokma.core import proctree

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids")


def test_a_reused_pid_is_not_killed():
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        tree = proctree.ProcessTree(os.getpid() + 10_000_000)       # a pid that does not exist: the group is skipped
        tree._known[other.pid] = "0"                                 # remembered with another start time: reused
        tree._signal_all(9)
        time.sleep(0.2)
        assert other.poll() is None and other.pid not in tree._known
    finally:
        other.kill()
        other.wait()


def test_a_remembered_pid_with_the_same_start_is_killed():
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        tree = proctree.ProcessTree(os.getpid() + 10_000_000)
        tree._note([other.pid])
        tree._signal_all(9)
        assert other.wait(timeout=5) != 0
    finally:
        other.kill()
        other.wait()
