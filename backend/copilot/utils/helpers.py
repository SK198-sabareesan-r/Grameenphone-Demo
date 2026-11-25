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
    """Pull the first complete JSON object out of a model reply.

    Text before or after the object is ignored, and so is a second object. A brace in the text before the
    real object does not stop the search: each "{" is tried in turn."""
    decoder = json.JSONDecoder()
    position = text.find("{")
    while position >= 0:
        try:
            value, _ = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            position = text.find("{", position + 1)
            continue
        if isinstance(value, dict):
            return value
        position = text.find("{", position + 1)
    raise ValueError("No JSON object found in model output")


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
