"""Evidence-backed running-configuration extraction (S4.2).

The reader owns three things the adapter does not:

* retries with a bounded delay, so one transient console answer does not
  become a permanent "configuration unavailable";
* the original raw text, written to an artifact file with a SHA-256 and
  recorded in the evidence store (the JSONL keeps the path/hash plus a
  preview, not a duplicated multi-kilobyte blob);
* the platform-specific honesty: an IOS read that returns boot text is
  UNKNOWN, an ASA rejection is UNKNOWN with its CommandStatus, and a Linksys
  is UNSUPPORTED with the measured reason. None of these become "empty" or
  "partial configuration" by accident.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..adapter.base import PacketTracerAdapter
from ..core.observability import ObservationStatus
from ..evidence.store import EvidenceStore
from .models import ConfigExtract, ConfigStatus, Platform, sha256_text
from .parser import IosConfigParser, ParsedConfig

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_component(value: str, *, fallback: str = "device") -> str:
    """A filesystem-safe path component; never lets `..` or separators through."""
    cleaned = _SAFE_NAME.sub("_", str(value)).strip("._")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned[:120]


@dataclass
class ConfigCorpus:
    """Every configuration read in one import, plus the parsed structures."""

    extracts: dict[str, ConfigExtract] = field(default_factory=dict)
    parsed: dict[str, ParsedConfig] = field(default_factory=dict)

    def status_of(self, device: str) -> ConfigStatus:
        extract = self.extracts.get(device)
        return extract.status if extract is not None else ConfigStatus.UNKNOWN

    def parsed_for(self, device: str) -> ParsedConfig | None:
        return self.parsed.get(device)

    def unparsed_count(self) -> int:
        return sum(len(config.unparsed) for config in self.parsed.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "extracts": {n: e.to_dict() for n, e in sorted(self.extracts.items())},
            "parsed": {n: p.to_dict() for n, p in sorted(self.parsed.items())},
            "unparsed_statements": self.unparsed_count(),
        }


class RunningConfigReader:
    """Read, retry, preserve and parse configurations."""

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        *,
        evidence: EvidenceStore | None = None,
        artifacts_root: Path | None = None,
        attempts: int = 2,
        retry_delay_s: float = 0.5,
        parser: IosConfigParser | None = None,
    ):
        self.adapter = adapter
        self.evidence = evidence
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.attempts = max(1, attempts)
        self.retry_delay_s = max(0.0, retry_delay_s)
        self.parser = parser or IosConfigParser()
        #: Parsed structures from the last `read(parse=True)` call, keyed by
        #: device. Kept beside the extract so a caller gets both without the
        #: reader re-parsing the same text.
        self.parsed: dict[str, ParsedConfig] = {}

    def read(
        self,
        device: str,
        *,
        platform: Platform | None = None,
        parse: bool = True,
    ) -> ConfigExtract:
        started = time.time()
        last_detail = ""
        attempts = 0
        for attempt in range(1, self.attempts + 1):
            attempts = attempt
            try:
                answer = self.adapter.read_running_config(device)
            except Exception as exc:  # a dead channel is a gap, not a crash
                answer = None
                last_detail = f"adapter raised {type(exc).__name__}: {exc}"
            else:
                last_detail = answer.detail or answer.status.value
            if answer is not None and answer.is_known:
                raw = str(answer.value)
                extract = ConfigExtract(
                    device=device,
                    platform=platform or Platform.UNKNOWN,
                    status=ConfigStatus.KNOWN,
                    raw=raw,
                    attempts=attempt,
                    elapsed_s=time.time() - started,
                    detail=None,
                    sha256=sha256_text(raw),
                    read_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    source=answer.source,
                )
                self._persist(extract)
                if parse:
                    parsed = self._parse(extract)
                    if parsed is not None:
                        self.parsed[device] = parsed
                return extract
            if answer is not None and answer.status is ObservationStatus.UNSUPPORTED:
                # Retrying a capability gap only wastes a console timeout.
                extract = ConfigExtract(
                    device=device,
                    platform=platform or Platform.UNKNOWN,
                    status=ConfigStatus.UNSUPPORTED,
                    attempts=attempts,
                    elapsed_s=time.time() - started,
                    detail=answer.detail or "platform has no configuration reader",
                )
                self._record(extract)
                return extract
            if attempt < self.attempts:
                time.sleep(self.retry_delay_s * attempt)

        extract = ConfigExtract(
            device=device,
            platform=platform or Platform.UNKNOWN,
            status=ConfigStatus.UNKNOWN,
            attempts=attempts,
            elapsed_s=time.time() - started,
            detail=last_detail or "configuration read returned no answer",
        )
        self._record(extract)
        return extract

    def read_many(
        self,
        devices: dict[str, Platform],
        *,
        parse: bool = True,
    ) -> ConfigCorpus:
        corpus = ConfigCorpus()
        for name in sorted(devices):
            extract = self.read(name, platform=devices[name], parse=parse)
            corpus.extracts[name] = extract
            if parse and name in self.parsed:
                corpus.parsed[name] = self.parsed[name]
        return corpus

    # -- internals ----------------------------------------------------------

    def _parse(self, extract: ConfigExtract) -> ParsedConfig | None:
        if not extract.raw:
            return None
        return self.parser.parse_extract(extract)

    def _persist(self, extract: ConfigExtract) -> None:
        """Write the raw text to the artifact directory and record its hash."""
        if self.artifacts_root is None:
            self._record(extract)
            return
        build_id = safe_component(
            self.evidence.build_id if self.evidence is not None else "import"
        )
        directory = self.artifacts_root / build_id / "configs"
        directory.mkdir(parents=True, exist_ok=True)
        name = safe_component(extract.device) + ".txt"
        path = directory / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(extract.raw, encoding="utf-8", newline="\n")
        tmp.replace(path)
        extract.raw_path = str(path)
        extract.sha256 = sha256_text(extract.raw)
        self._record(extract)

    def _record(self, extract: ConfigExtract) -> None:
        if self.evidence is None:
            return
        self.evidence.record(
            "brownfield_config_read",
            {
                **extract.to_dict(),
                "raw_preview": extract.raw[:2000],
                "raw_truncated": len(extract.raw) > 2000,
            },
            component="brownfield.config_reader",
        )
