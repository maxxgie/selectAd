"""Test user generator.

Produces realistic, unique test user records for signup QA automation.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Set

from faker import Faker

COUNTRY_ALIASES = {
    "US": "United States",
    "USA": "United States",
    "UNITED STATES OF AMERICA": "United States",
    "UK": "United Kingdom",
    "GB": "United Kingdom",
}


def normalize_country(country: str) -> str:
    value = (country or "").strip()
    return COUNTRY_ALIASES.get(value.upper(), value)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    # AdSelect accepts mobile numbers with 6–12 digits; use 10-digit US numbers.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) > 10:
        digits = digits[-10:]
    if len(digits) < 6:
        digits = (digits + "3074106456")[:10]
    return digits[:10]


@dataclass
class TestUser:
    """A single generated test user record."""

    id: str
    full_name: str
    username: str
    email: str
    country: str
    phone: str
    password: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        """Return the user as a plain dictionary suitable for storage/export."""
        return {
            "id": self.id,
            "full_name": self.full_name,
            "username": self.username,
            "email": self.email,
            "country": self.country,
            "phone": self.phone,
            "password": self.password,
            "created_at": self.created_at,
        }


class UserGenerator:
    """Generates unique, realistic test users."""

    def __init__(self, country: str = "United States", default_password: str = "12345678", username_length: int = 8) -> None:
        self.faker = Faker()
        self.country = normalize_country(country)
        self.default_password = default_password
        self.username_length = max(1, int(username_length))
        self._used_usernames: Set[str] = set()
        self._used_emails: Set[str] = set()

    def generate(self, count: int) -> List[TestUser]:
        """Generate ``count`` unique test users."""
        if count < 1:
            raise ValueError("count must be a positive integer")
        return [self._generate_one() for _ in range(count)]

    def _generate_one(self) -> TestUser:
        now = datetime.now(timezone.utc).isoformat()
        return TestUser(
            id=str(uuid.uuid4()),
            full_name=self.faker.name(),
            username=self._unique_username(),
            email=self._unique_email(),
            country=self.country,
            phone=normalize_phone(self.faker.phone_number()),
            password=self.default_password,
            created_at=now,
        )

    def _unique_username(self) -> str:
        for _ in range(100):
            username = "".join(self.faker.random_elements(elements=("a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"), length=self.username_length, unique=False))
            if username not in self._used_usernames:
                self._used_usernames.add(username)
                return username
        raise RuntimeError("Unable to generate a unique username after 100 attempts")

    def _unique_email(self) -> str:
        for _ in range(100):
            local = "".join(self.faker.random_elements(elements=("a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"), length=self.username_length, unique=False))
            email = f"{local}@gmail.com"
            if email not in self._used_emails:
                self._used_emails.add(email)
                return email
        raise RuntimeError("Unable to generate a unique email after 100 attempts")
