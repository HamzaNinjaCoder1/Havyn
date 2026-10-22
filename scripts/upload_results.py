"""Upload a results directory through the runner's verified uploader.

    python scripts/upload_results.py runner_results --destination <dir>

The destination defaults to ``$HAVYN_UPLOAD_DIR``. The copy is read back and
SHA-256 verified by ``make_dir_uploader``; a missing destination or a short
copy is reported, never turned into success. Exit 1 when the upload did not
arrive, 0 when it did or when no destination is configured (the caller can
distinguish the two from the JSON).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EXIT_OK = 0
EXIT_UPLOAD_FAILED = 1


def upload(results_dir: Path, destination: Path) -> dict:
    from scripts.havyn_runner import make_dir_uploader

    return make_dir_uploader(destination)(results_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="upload-results")
    parser.add_argument("results_dir")
    parser.add_argument(
        "--destination",
        default=os.environ.get("HAVYN_UPLOAD_DIR"),
        help="destination directory (default: $HAVYN_UPLOAD_DIR)",
    )
    args = parser.parse_args(argv)

    if not args.destination:
        print(json.dumps(
            {"uploaded": False, "reason": "no HAVYN_UPLOAD_DIR configured"}
        ))
        return EXIT_OK

    outcome = upload(Path(args.results_dir), Path(args.destination))
    print(json.dumps(outcome))
    return EXIT_OK if outcome.get("uploaded") else EXIT_UPLOAD_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
