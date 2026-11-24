"""Conversions between plain Python data and what DynamoDB accepts."""

from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def to_dynamo(value: Any) -> Any:
    """float becomes Decimal, because DynamoDB rejects float. Recurses into dicts and lists."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Decimal becomes int when whole, else float, so the data can be sent to the panel as JSON."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def serialize_item(item: dict) -> dict:
    """A plain dict as the low-level DynamoDB wire format ({"name": {"S": "Rahim"}, ...})."""
    return {key: _SERIALIZER.serialize(to_dynamo(value)) for key, value in item.items()}


def deserialize_item(item: dict) -> dict:
    """The low-level DynamoDB wire format as a plain dict."""
    return {key: from_dynamo(_DESERIALIZER.deserialize(value)) for key, value in item.items()}
