"""Trace：把每一步决策写成 JSONL，并在收尾时补齐 next_action。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def finalize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """next_action 由「下一步实际做了什么」回填，避免自述与实际不符。"""
    for index, row in enumerate(rows):
        row["next_action"] = rows[index + 1]["action"] if index + 1 < len(rows) else None
    return rows


def write_trace(path: Path, rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in finalize_rows(rows):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.write(json.dumps({"event": "summary", **summary}, ensure_ascii=False) + "\n")
    return path
