"""Transcript persistence for gdb/MI debug sessions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kdive.providers.local_libvirt.debug.mi_protocol import MiRecord
from kdive.security.secrets.redaction import Redactor


def append_transcript(
    *, transcript_path: Path, command: str, records: list[MiRecord], redactor: Redactor
) -> None:
    """Append one redacted JSON-lines record per MI command to the session transcript."""
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "observed_at": datetime.now(UTC).isoformat(),
        "command": command,
        "records": [record.model_dump(mode="json") for record in records],
    }
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redactor.redact_value(entry), default=str))
        handle.write("\n")
