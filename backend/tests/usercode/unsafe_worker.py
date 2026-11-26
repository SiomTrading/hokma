"""A worker for the adversarial tests ONLY: the same worker, with the static check turned off and the dangerous standard modules already loaded.

It plays the strongest attacker: a rule that got past the static check (the check is the first layer, never the only one) in a process where
socket, subprocess, ctypes, shutil, tempfile and io are already imported. What stops it is what the worker adds at run time: the restricted builtins
and importer, the audit hook, the limits and the namespaces. Run by file path, never imported by the package.
"""
import ctypes  # noqa: F401
import sys
import glob  # noqa: F401
import io  # noqa: F401
import os  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401

from hokma.factory.usercode import static_check, worker

if __name__ == "__main__":
    # `--no-landlock` proves the audit hook on its own, as nohook_worker.py proves the network namespace on its own
    worker.main(check=lambda source, kind: static_check.CheckResult(True, []), landlock="--no-landlock" not in sys.argv[1:])
