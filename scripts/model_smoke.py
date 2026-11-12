"""Download the real embedding model and run it:  python scripts/model_smoke.py [--directory DIR]

Used by the `model-smoke` workflow on each platform. Prints the seconds spent downloading and loading and checks
that a question in one language is closer to its passage in another than to an unrelated one.
Exit code 1 when the download, the checksum or the model fails.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hokma.library.embedder import Embedder  # noqa: E402
from hokma.library.model_download import ModelDownloader, load_manifest  # noqa: E402

DEADLINE_SECONDS = 1800


def run(directory: Path) -> int:
    manifest = load_manifest()
    downloader = ModelDownloader(manifest, directory / manifest["id"], waits=(5,))
    started = time.monotonic()
    downloader.start()
    while not downloader.is_ready():
        status = downloader.status()
        if status["state"] == "failed" or time.monotonic() - started > DEADLINE_SECONDS:
            downloader.stop()
            print(f"model download did not finish: {status}", file=sys.stderr)
            return 1
        time.sleep(1)
    downloaded = time.monotonic() - started
    names = [entry["name"] for entry in manifest["files"]]
    embedder = Embedder(downloader.path_of(names[0]), downloader.path_of(names[1]))
    loaded_at = time.monotonic()
    try:
        question = embedder.embed_query("how long should bread rise?")
        passages = embedder.embed_passages(["The dough rests for two hours before baking.", "Invoices are due in 30 days."])
    finally:
        embedder.close()
    inference = time.monotonic() - loaded_at
    related, unrelated = float(passages[0] @ question), float(passages[1] @ question)
    print(f"download {downloaded:.1f}s, first load and inference {inference:.1f}s, "
          f"related {related:.3f}, unrelated {unrelated:.3f}")
    if related <= unrelated:
        print("the related passage is not closer than the unrelated one", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=None)
    args = parser.parse_args()
    if args.directory is not None:
        args.directory.mkdir(parents=True, exist_ok=True)
        return run(args.directory)
    with tempfile.TemporaryDirectory() as tmp:
        return run(Path(tmp))


if __name__ == "__main__":
    sys.exit(main())
