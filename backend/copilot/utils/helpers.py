"""Small shared helpers: JSON loading, JSON extraction, prompt rendering."""

import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start : end + 1])


def render_prompt(template: str, **values: Any) -> str:
    """Replace {{key}} placeholders. Non-string values are dumped as JSON."""
    out = template
    for key, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        out = out.replace("{{" + key + "}}", text)
    return out


def contains_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)
