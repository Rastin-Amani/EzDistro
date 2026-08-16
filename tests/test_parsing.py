import pytest

from app.domain.parsing import extract_json


def test_fenced_json():
    raw = '```json\n{"title": "x", "sections": []}\n```'
    assert extract_json(raw) == {"title": "x", "sections": []}


def test_json_with_surrounding_prose():
    raw = 'Here is your outline:\n{"title": "a", "sections": [1, 2]}\n\nThanks!'
    assert extract_json(raw) == {"title": "a", "sections": [1, 2]}


def test_nested_braces_and_strings_with_braces():
    raw = '{"a": {"b": "}"}, "c": [1, {"d": "{"}]}'
    assert extract_json(raw) == {"a": {"b": "}"}, "c": [1, {"d": "{"}]}


def test_array_document():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]


def test_escaped_quotes_inside_strings():
    raw = '{"text": "she said \\"hi\\""}'
    assert extract_json(raw) == {"text": 'she said "hi"'}


def test_garbage_raises():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_empty_raises():
    with pytest.raises(ValueError):
        extract_json("")
