"""Mock GP customer directory and network notices."""

from config.settings import Settings
from copilot.utils.helpers import load_json


def _normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


class CustomerDirectory:
    def __init__(self, settings: Settings) -> None:
        mock_dir = settings.data_dir / "mock"
        self._customers = {c["id"]: c for c in load_json(mock_dir / "customers.json")}
        self._notices = load_json(mock_dir / "network_notices.json")
        self._phone_map = settings.phone_map
        self._default_id = settings.demo_default_customer_id
        self.override_id: str | None = None

    def all(self) -> list[dict]:
        return list(self._customers.values())

    def get(self, customer_id: str) -> dict | None:
        return self._customers.get(customer_id)

    def resolve(self, caller_number: str) -> dict | None:
        """Pick the customer for a call: UI override, then phone map, then default."""
        if self.override_id and self.override_id in self._customers:
            return self._customers[self.override_id]
        mapped = self._phone_map.get(_normalize_phone(caller_number))
        return self._customers.get(mapped or self._default_id)

    def notice_for(self, customer: dict | None) -> dict | None:
        return self._notices.get(customer["area"]) if customer else None
