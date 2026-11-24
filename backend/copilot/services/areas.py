"""Mock network status by area. The agent can check any area the customer names."""

import re
from pathlib import Path

from copilot.utils.helpers import load_json

NO_NOTICE = "No network notice on file for this area"
_SEPARATORS = re.compile(r"[\s.,;:!?\"'()\[\]{}\-/।]+")


def normalize_text(text: str) -> str:
    """Lower-case and collapse separators. Bengali letters and vowel signs are left untouched."""
    return _SEPARATORS.sub(" ", (text or "").lower()).strip()


class NetworkAreas:
    def __init__(self, path: Path) -> None:
        self._areas: dict[str, dict] = load_json(path)["areas"]
        pairs = [
            (normalize_text(alias), name)
            for name, info in self._areas.items()
            for alias in {name, *info.get("aliases", [])}
        ]
        self._aliases = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)  # longest match first

    def names(self) -> list[str]:
        return list(self._areas)

    def resolve(self, text: str) -> str | None:
        """Area for what the agent typed: an exact name or alias, or one mentioned inside a sentence."""
        norm = normalize_text(text)
        if not norm:
            return None
        for alias, name in self._aliases:
            if alias == norm:
                return name
        padded = f" {norm} "
        for alias, name in self._aliases:
            # Latin aliases must be whole words. Bengali words take suffixes (মিরপুরে), so match inside.
            if (f" {alias} " in padded) if alias.isascii() else (alias in norm):
                return name
        return None

    def status(self, area_text: str) -> dict:
        """Status for one area. An area with no notice says so and does not claim the network is fine."""
        name = self.resolve(area_text) or area_text.strip()
        notice = (self._areas.get(name) or {}).get("notice")
        if notice:
            return {"area": name, **notice}
        return {"area": name, "status": "no_notice", "message": NO_NOTICE, "until": None}
