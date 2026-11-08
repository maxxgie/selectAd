"""JSON exporter for test users."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from generators.user_generator import TestUser


class JSONExporter:
    """Exports test users to a JSON file."""

    def __init__(self, output_path: str = "output/test_users.json") -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, users: List[TestUser]) -> Path:
        """Write users to JSON and return the output path."""
        data = [user.to_dict() for user in users]
        with self.output_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        return self.output_path

    def import_users(self, input_path: str | Path | None = None) -> List[TestUser]:
        """Load users from a JSON file."""
        path = Path(input_path) if input_path else self.output_path
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [TestUser(**item) for item in data]
