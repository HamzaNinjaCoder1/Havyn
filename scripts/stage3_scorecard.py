"""Stage 3 scorecard: the 53-track status roll-up and its evidence.

    python scripts/stage3_scorecard.py

Reads the generated capability map and writes
``docs/reports/stage3-scorecard.{md,json}``. No status is invented here; the
counts are exactly the rows in the map.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
MAP = ROOT / "docs" / "capability-map.json"
OUT_MD = ROOT / "docs" / "reports" / "stage3-scorecard.md"
OUT_JSON = ROOT / "docs" / "reports" / "stage3-scorecard.json"


def main() -> int:
    data = json.loads(MAP.read_text(encoding="utf-8"))
    rows = [row for row in data["features"] if row["id"].startswith("S3.")]
    counts = Counter(row["status"] for row in rows)
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "generated": generated,
        "total": len(rows),
        "verified": counts.get("Verified", 0),
        "refused": counts.get("Refused", 0),
        "unverified": counts.get("Unverified", 0),
        "rows": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        "# Havyn Stage 3 scorecard",
        "",
        f"_generated {generated}_",
        "",
        f"Tracks: **{len(rows)}** · Verified **{payload['verified']}** · "
        f"Refused **{payload['refused']}** · Unverified **{payload['unverified']}**",
        "",
        "| track | status | evidence |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        evidence = str(row["evidence"]).replace("|", "\\|")
        lines.append(f"| {row['id']} | {row['status']} | `{evidence}` |")
    lines.extend([
        "",
        "## How to read this",
        "",
        "- **Verified** — a live lab or live probe in this repository proves the",
        "  capability that the row names. Rows that say `config/read-back` prove",
        "  the configuration and the device's own `show` output, not a",
        "  failure-mode behaviour. Existing code is not, by itself, verification.",
        "- **Refused** — PT was probed and lacks/blocks the required capability",
        "  (or it did not survive the push); Havyn intentionally refuses it at",
        "  compile-time/runtime rather than producing a false PASS. The exact",
        "  reason and the alternative are in the row's evidence file.",
        "- **Unverified** — not proven. The evidence pointer says exactly what is",
        "  missing. No status is inferred from a compile or a push receipt.",
        "- **Refused is not Failed** — Refused is an evidence-backed capability",
        "  gap in PT; a Failed run is a test/build that did not pass.",
        "- **GUI vs automation** — a feature that can be configured manually in",
        "  the PT GUI is still Refused here when Havyn's Script Engine/API cannot",
        "  prove it. Havyn only claims what its automation path observed.",
        "",
        "## Known evidence defect",
        "",
        "The A5/A6/A7/A9 live labs show a PASSing flow while the adapter's",
        "static-route read-back still reports the route missing after retry.",
        "The flow is the stronger proof (traffic crossed); the read-back defect",
        "is recorded rather than hidden and is a separate adapter fix.",
        "",
    ])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(rows)} tracks {dict(counts)} -> {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
