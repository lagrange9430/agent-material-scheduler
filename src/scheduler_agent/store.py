"""数据读取与草稿持久化。

草稿是本项目唯一的持久化状态；幂等完全依赖它，因此写入必须原子。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class DataStore:
    """只读的合成数据：素材、规则、排期表。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._materials = read_json(self.data_dir / "materials.json")
        self._rules = read_json(self.data_dir / "rules.json")
        self._schedule_cache: Dict[str, Dict[str, Any]] = {}

    def material(self, material_id: str) -> Optional[Dict[str, Any]]:
        for item in self._materials.get("materials", []):
            if item.get("material_id") == material_id:
                return item
        return None

    @property
    def rules(self) -> List[Dict[str, Any]]:
        return self._rules.get("rules", [])

    def schedule(self, filename: str = "schedule.json") -> Dict[str, Any]:
        if filename not in self._schedule_cache:
            self._schedule_cache[filename] = read_json(self.data_dir / filename)
        return self._schedule_cache[filename]

    def booked_slots(self, filename: str = "schedule.json") -> Dict[str, str]:
        schedule = self.schedule(filename)
        return {row["slot"]: row["material_id"] for row in schedule.get("booked", [])}

    def booked_materials(self, filename: str = "schedule.json") -> List[str]:
        return [row["material_id"] for row in self.schedule(filename).get("booked", [])]


def idempotency_key(material_id: str, date: str, slot: str) -> str:
    raw = f"{material_id}|{date}|{slot}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def draft_id_for(key: str) -> str:
    return f"D-{key[:8]}"


class DraftStore:
    """草稿文件：runtime/drafts.json，key 为幂等键。"""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / "drafts.json"

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"drafts": {}}
        payload = read_json(self.path)
        payload.setdefault("drafts", {})
        return payload

    def find(self, key: str) -> Optional[Dict[str, Any]]:
        return self.load()["drafts"].get(key)

    def count(self) -> int:
        return len(self.load()["drafts"])

    def put(self, key: str, record: Dict[str, Any]) -> None:
        payload = self.load()
        payload["drafts"][key] = record
        write_json_atomic(self.path, payload)

    def all(self) -> List[Dict[str, Any]]:
        return list(self.load()["drafts"].values())
