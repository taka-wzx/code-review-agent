"""JSONL session logging (project convention: one JSON object per line)."""

import json
from pathlib import Path


def save_jsonl(records, path):
    """Write records to a JSONL file, one JSON object per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_jsonl(path):
    """Read a JSONL file back into a list of dicts; missing file -> []."""
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def append_jsonl(record, path):
    """Append one record to a session log created by save_jsonl."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
