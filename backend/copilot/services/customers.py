"""Mock GP customer directory. A customer is found by mobile number, never listed or guessed."""

import re

from config.settings import Settings
from copilot.utils.helpers import load_json

MIN_DIGITS, MAX_DIGITS = 10, 13


def normalize_number(value: str) -> str:
    """One format for every mobile number: digits only, 01XXXXXXXXX. Accepts +880..., 880... and 1XXXXXXXXX."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("880") and len(digits) == 13:
        return "0" + digits[3:]
    if len(digits) == 10 and digits.startswith("1"):
        return "0" + digits
    return digits


def is_plausible_number(value: str) -> bool:
    return MIN_DIGITS <= len(re.sub(r"\D", "", value or "")) <= MAX_DIGITS


class CustomerDirectory:
    def __init__(self, settings: Settings) -> None:
        customers = load_json(settings.data_dir / "mock" / "customers.json")
        self._by_id = {c["id"]: c for c in customers}
        self._by_number = {normalize_number(c["msisdn"]): c for c in customers}
        # Verified team phones used in the demo count as that customer's mobile number.
        for phone, customer_id in settings.phone_map.items():
            if customer_id in self._by_id:
                self._by_number[normalize_number(phone)] = self._by_id[customer_id]

    def find_by_number(self, mobile_number: str) -> dict | None:
        return self._by_number.get(normalize_number(mobile_number))
