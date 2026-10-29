"""Cheap Hugging Face connectivity/authentication smoke test.

It never downloads model weights. It does:
  1. token presence (HF_TOKEN / Colab secret), without printing it;
  2. Hub authentication (whoami) when a token exists;
  3. metadata resolution for the tiny smoke-test model;
  4. tokenizer load for that tiny model (a few small files);
  5. metadata resolution for a registry model, and a disk-size estimate.

Usage:
    python hf_smoke_test.py --dry-run          # no network, plan only
    python hf_smoke_test.py                    # tiny public model only
    python hf_smoke_test.py --registry-model Qwen/Qwen3-8B --require-model-access
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "havyn-stage5" / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "havyn-stage5" / "scripts"))

from hf_auth import get_token, status, whoami  # noqa: E402

TINY_MODEL = "hf-internal-testing/tiny-random-LlamaForCausalLM"

# The Hub can hang behind a blocked network; fail with a clear message instead.
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    fatal: bool = True


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, fatal: bool = True) -> None:
        self.results.append(Result(name, ok, detail, fatal))

    @property
    def passed(self) -> bool:
        return all(r.ok for r in self.results if r.fatal)


def _human_size(size: int | None) -> str:
    if not size:
        return "unknown"
    gb = size / 1e9
    return f"{gb:.2f} GB" if gb >= 1 else f"{size / 1e6:.1f} MB"


def _model_disk_estimate(api, model_id: str) -> int | None:
    try:
        info = api.model_info(model_id, files_metadata=True)
    except Exception:
        return None
    total = 0
    for sibling in info.siblings or []:
        total += getattr(sibling, "size", None) or 0
    return total or None


def run(dry_run: bool, registry_model: str | None,
        require_model_access: bool) -> Report:
    report = Report()
    auth = status()
    report.add(
        "hf_token",
        True,
        f"present from {auth.source}" if auth.present else "absent (public models still work)",
        fatal=False,
    )

    try:
        import huggingface_hub  # noqa: F401
    except ImportError as exc:
        report.add("huggingface_hub", False, f"not installed: {exc}")
        return report
    report.add("huggingface_hub", True, "imported")

    if dry_run:
        report.add(
            "network",
            True,
            "skipped by --dry-run (would check whoami, tiny tokenizer, model metadata)",
            fatal=False,
        )
        return report

    print("[smoke] checking authentication ...", flush=True)
    if auth.present:
        try:
            identity = whoami()
            report.add("whoami", True, f"authenticated as {identity['name']} ({identity['type']})")
        except Exception as exc:  # noqa: BLE001
            report.add("whoami", False, f"token rejected or Hub unreachable: {exc}")
    else:
        report.add("whoami", True, "skipped (no token; public access only)", fatal=False)

    print("[smoke] resolving tiny model metadata ...", flush=True)
    try:
        from huggingface_hub import HfApi

        api = HfApi(token=get_token(required=False))
        info = api.model_info(TINY_MODEL)
        report.add("tiny_model_metadata", True, f"resolved {info.id} @ {info.sha[:12]}")
    except Exception as exc:  # noqa: BLE001
        report.add("tiny_model_metadata", False, f"cannot resolve {TINY_MODEL}: {exc}")
        return report

    print("[smoke] loading tiny tokenizer ...", flush=True)
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(TINY_MODEL)
        report.add(
            "tiny_tokenizer",
            True,
            f"loaded {type(tokenizer).__name__} (vocab {tokenizer.vocab_size})",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("tiny_tokenizer", False, f"tokenizer load failed: {exc}")

    if registry_model:
        try:
            size = _model_disk_estimate(api, registry_model)
            report.add(
                "registry_model_access",
                True,
                f"{registry_model} resolvable; full weights ~{_human_size(size)}",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                "registry_model_access",
                not require_model_access,
                f"cannot resolve {registry_model}: {exc}. "
                "Gated models need HF access approval plus a token.",
                fatal=require_model_access,
            )

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="no network calls")
    parser.add_argument("--registry-model", default="__registry__",
                        help="check metadata/disk size (default: first standard registry candidate)")
    parser.add_argument("--no-registry-model", action="store_true",
                        help="skip the registry model metadata/disk check")
    parser.add_argument("--require-model-access", action="store_true",
                        help="fail if the registry model is not resolvable")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    registry_model = args.registry_model
    if registry_model == "__registry__" and not args.no_registry_model:
        try:
            import yaml
            registry = yaml.safe_load(
                (REPO / "havyn-stage5" / "configs" / "models.yaml")
                .read_text(encoding="utf-8"))
            registry_model = registry["standard_candidates"][0]["model_id"]
        except Exception:
            registry_model = None
    elif args.no_registry_model:
        registry_model = None

    report = run(args.dry_run, registry_model, args.require_model_access)
    if args.json:
        print(json.dumps({
            "passed": report.passed,
            "results": [r.__dict__ for r in report.results],
        }, indent=2))
    else:
        for result in report.results:
            mark = "OK  " if result.ok else ("FAIL" if result.fatal else "WARN")
            print(f"[{mark}] {result.name}: {result.detail}")
        print("PASS" if report.passed else "FAIL")
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
