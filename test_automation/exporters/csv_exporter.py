"""CSV exporter for test users."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import List

from generators.user_generator import TestUser


class CSVExporter:
    """Exports test users to a CSV file."""

    FIELDNAMES = [
        "id",
        "full_name",
        "username",
        "email",
        "country",
        "phone",
        "password",
        "created_at",
    ]

    def __init__(self, output_path: str = "output/test_users.csv") -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, users: List[TestUser]) -> Path:
        """Write users to CSV and return the output path."""
        with self.output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.FIELDNAMES)
            writer.writeheader()
            for user in users:
                writer.writerow(user.to_dict())
        return self.output_path

    def import_users(self, input_path: str | Path | None = None) -> List[TestUser]:
        """Load users from a CSV file."""
        path = Path(input_path) if input_path else self.output_path
        users: List[TestUser] = []
        with path.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                users.append(TestUser(**row))
        return users
