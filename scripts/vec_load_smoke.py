"""Load `sqlite-vec` with the running Python and ask for the nearest vector:  python scripts/vec_load_smoke.py

CI runs it with the Python that Hokma installs for the student (a uv-managed CPython), not the runner's own. Exit code 1
and one line on stderr when this Python cannot load SQLite extensions or the extension does not load.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hokma.db.vecext import check_support  # noqa: E402


def main() -> int:
    support = check_support()
    where = f"{sys.implementation.name} {sys.version.split()[0]} at {sys.executable}"
    if not support.ok:
        print(f"sqlite-vec cannot be used with {where}: {support.code}: {support.detail}", file=sys.stderr)
        return 1
    print(f"sqlite-vec loads and answers a nearest-vector query with {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
