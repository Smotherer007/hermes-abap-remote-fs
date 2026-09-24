"""Shared plumbing for the tool modules.

Hermes hands a handler the raw arguments the model produced and expects a
JSON string back -- also on failure, a handler must never raise. Models also
send numbers as strings and booleans as "true" now and then, so every tool
reads its arguments through the coercing helpers below instead of trusting
the schema.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, Dict, List, Optional

from ..models import ToolInputError

PROFILE_PROPERTY = {
    "type": "string",
    "description": "Profile name to use. Uses active profile if omitted.",
}

#: The three ways to address an object, shared by most tools.
OBJECT_PROPERTIES = {
    "name": {"type": "string", "description": "Object name, e.g. 'ZCL_MY_CLASS'"},
    "objectUrl": {
        "type": "string",
        "description": "Object URL, e.g. '/sap/bc/adt/oo/classes/zcl_my_class'. Takes precedence over name.",
    },
    "type": {"type": "string", "description": "Optional type hint for search, e.g. 'CLAS/OC'."},
}


def schema(name: str, description: str, properties: Dict[str, Any], required: List[str] = ()) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(required),
        },
    }


def result(text: str, **details: Any) -> str:
    """Success: the formatted text the model reads, plus structured details."""
    return json.dumps({"result": text, **details}, ensure_ascii=False, default=str)


def error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def tool_handler(fn: Callable[[dict], str]) -> Callable[..., str]:
    """Wrap a handler so every exception becomes an ``{"error": ...}`` result."""

    @functools.wraps(fn)
    def wrapper(args: Optional[dict] = None, **_kwargs: Any) -> str:
        try:
            return fn(args if isinstance(args, dict) else {})
        except Exception as exc:  # the contract: never raise into the agent loop
            return error(str(exc) or type(exc).__name__)

    return wrapper


# Argument coercion


def opt_str(args: dict, key: str) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        raise ToolInputError(f"{key} must be a string.")
    return str(value)


def req_str(args: dict, key: str) -> str:
    value = opt_str(args, key)
    if value is None or not value.strip():
        raise ToolInputError(f"{key} must not be empty.")
    return value


def opt_int(args: dict, key: str) -> Optional[int]:
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ToolInputError(f"{key} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"{key} must be a number.") from None
    if number != int(number):
        raise ToolInputError(f"{key} must be a whole number.")
    return int(number)


def opt_bool(args: dict, key: str) -> Optional[bool]:
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "0"):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise ToolInputError(f"{key} must be true or false.")


def opt_str_list(args: dict, key: str) -> Optional[List[str]]:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        # "draft,archived" or a JSON array that arrived as a string
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else [value]
        except ValueError:
            value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        raise ToolInputError(f"{key} must be a list of strings.")
    return [str(item) for item in value]


def object_args(args: dict) -> dict:
    """name / objectUrl / type as keyword arguments for resolve_object."""
    name = opt_str(args, "name")
    object_url = opt_str(args, "objectUrl")
    if not name and not object_url:
        raise ToolInputError("Provide either 'name' or 'objectUrl' to address an object.")
    return {"name": name, "object_url": object_url, "type_hint": opt_str(args, "type")}
