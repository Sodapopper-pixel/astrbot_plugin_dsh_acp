from __future__ import annotations

import copy
import json
import os


class ChatRegistry:
    def __init__(self, path: str):
        self._path = path
        self._records: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            data = None
        self._records = data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict | None:
        record = self._records.get(key)
        return copy.deepcopy(record) if isinstance(record, dict) else None

    def put(self, key: str, record: dict) -> None:
        self._records[key] = record
        self.save()

    def all(self) -> dict:
        return copy.deepcopy(self._records)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._records, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)
