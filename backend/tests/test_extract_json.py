"""extract_json must survive the ways a model wraps or extends its JSON."""
import pytest

from copilot.utils.helpers import extract_json


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_code_fence_and_text_around_the_object():
    assert extract_json('Here you go:\n```json\n{"a": "b"}\n```\nHope that helps.') == {"a": "b"}


def test_a_second_object_after_the_first_is_ignored():
    assert extract_json('{"mood": "calm"}\n{"mood": "angry"}') == {"mood": "calm"}


def test_text_with_braces_after_the_object_is_ignored():
    assert extract_json('{"answer": "Dial *121#"} (note: {see article})') == {"answer": "Dial *121#"}


def test_a_brace_before_the_real_object_is_skipped():
    assert extract_json('Use {braces} sparingly. {"ok": true}') == {"ok": True}


def test_braces_and_quotes_inside_strings_are_kept():
    assert extract_json('{"text": "a } b { c \\"d\\""}') == {"text": 'a } b { c "d"'}


def test_nested_objects():
    assert extract_json('{"a": {"b": [1, {"c": 2}]}} trailing') == {"a": {"b": [1, {"c": 2}]}}


@pytest.mark.parametrize("text", ["", "no json here", "{not json", "[1, 2, 3]"])
def test_no_object_raises(text):
    with pytest.raises(ValueError):
        extract_json(text)
