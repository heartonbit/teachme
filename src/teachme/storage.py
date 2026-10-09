import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    serialized = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, serialized)


def iso_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")