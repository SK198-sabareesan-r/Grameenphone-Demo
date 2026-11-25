"""Network status by area. Areas and their names are loaded once. A status is read fresh each time it is
checked, so a notice edited in the table shows up on the next check."""

import re

from copilot.repo.base import Repo

NO_NOTICE = "No network notice on file for this area"
BANGLISH_SUFFIXES = ("", "e", "te", "er", "ey")  # "Mirpur e", "Mirpure", "Mirpurer" are all Mirpur
_SEPARATORS = re.compile(r"[\s.,;:!?\"'()\[\]{}\-/।]+")


def normalize_text(text: str) -> str:
    """Lower-case and collapse separators. Bengali letters and vowel signs are left untouched."""
    return _SEPARATORS.sub(" ", (text or "").lower()).strip()


class NetworkAreas:
    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._names: list[str] = []
        self._aliases: list[tuple[str, str]] = []
        self._loaded = False

    async def load(self) -> None:
        """Read every area (a handful of rows) so typed names can be matched without a lookup per keystroke."""
        rows = await self._repo.list_areas()
        pairs = [
            (normalize_text(alias), row["area"])
            for row in rows
            for alias in {row["area"], *row.get("aliases", [])}
        ]
        self._aliases = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)  # longest match first
        self._names = sorted(row["area"] for row in rows)
        self._loaded = True

    async def ensure_loaded(self) -> None:
        if not self._loaded:
            await self.load()

    def names(self) -> list[str]:
        return list(self._names)

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
            if self._mentions(alias, norm, padded):
                return name
        return None

    @staticmethod
    def _mentions(alias: str, norm: str, padded: str) -> bool:
        if not alias.isascii():
            return alias in norm
        return any(f" {alias}{suffix} " in padded for suffix in BANGLISH_SUFFIXES)

    async def status(self, area_text: str) -> dict:
        """Status for one area. An area with no notice says so and does not claim the network is fine."""
        await self.ensure_loaded()
        name = self.resolve(area_text) or area_text.strip()
        row = await self._repo.get_area(name)
        notice = (row or {}).get("notice")
        if notice:
            return {"area": name, **notice}
        return {"area": name, "status": "no_notice", "message": NO_NOTICE, "until": None}
