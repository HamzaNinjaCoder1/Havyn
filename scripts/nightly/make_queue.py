"""Build a runner queue from the topology library (S2.3).

Active specs become cases; pending ones are listed as skipped-with-reason
so the morning report shows what the night did NOT cover and why.

    python scripts/nightly/make_queue.py [--out nightly-queue.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
LIBRARY = REPO_ROOT / "library"


def build_queue(library_dir: Path = LIBRARY) -> dict:
    """Queue cases for every active library spec + the skip record."""
    manifest = json.loads((library_dir / "manifest.json").read_text(
        encoding="utf-8"))
    cases: list[dict] = []
    skipped: list[dict] = []
    for entry in manifest["specs"]:
        spec_path = library_dir / entry["file"]
        if not spec_path.is_file():
            skipped.append({"id": entry["id"],
                            "reason": f"spec file missing: {entry['file']}"})
            continue
        if entry["status"] == "pending":
            skipped.append({"id": entry["id"],
                            "reason": entry.get("pending_reason",
                                                "pending")})
            continue
        cases.append({
            "id": entry["id"],
            "spec": str(spec_path),
            "safety_mode": "autonomous",
        })
    return {"cases": cases, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_queue")
    parser.add_argument("--out", default="nightly-queue.json")
    parser.add_argument("--library", default=str(LIBRARY))
    args = parser.parse_args(argv)

    queue = build_queue(Path(args.library))
    Path(args.out).write_text(json.dumps(queue, indent=1), encoding="utf-8")
    print(f"{len(queue['cases'])} cases, "
          f"{len(queue['skipped'])} skipped -> {args.out}")
    for skip in queue["skipped"]:
        print(f"  SKIP {skip['id']}: {skip['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
