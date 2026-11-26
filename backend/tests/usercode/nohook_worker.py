"""A worker for ONE adversarial test: no audit hook, so the layer of the operating system (the network namespace) is proved on its own."""
import socket  # noqa: F401

from hokma.factory.usercode import static_check, worker

if __name__ == "__main__":
    worker.main(check=lambda source, kind: static_check.CheckResult(True, []), install_audit_hook=False)
