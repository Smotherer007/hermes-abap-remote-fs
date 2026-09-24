"""Guard rails for changes, enforced through Hermes' ``pre_tool_call`` hook.

Settings under ``plugins.entries.abap-remote-fs.settings`` in ``config.yaml``
(or the Desktop settings form):

``safety_level`` -- for lock, write, text elements, object creation, and unit
tests that include dangerous/critical risk levels:

* ``open`` (default): run directly, as in pi-abap-fs.
* ``confirm``: every such call goes through Hermes' human-approval gate.
* ``readonly``: blocked.

``activation`` -- for ``abap_activate``:

* ``inherit`` (default): same as ``safety_level``.
* ``confirm``: always ask a human, whatever ``safety_level`` says.
* ``block``: never; activation is done by a person in SAP. This is the rule
  neo runs with.

``readonly_profiles`` -- profile names (e.g. ``prod``) where every change is
blocked, whatever the other settings say. Calls without a ``profile``
argument count as the active profile.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from .tools import ACTIVATE_TOOL, WRITE_TOOLS

LEVELS = ("open", "confirm", "readonly")
ACTIVATION = ("inherit", "confirm", "block")
PLUGIN = "abap-remote-fs"


def normalize_level(value: Any) -> str:
    level = str(value or "open").strip().lower()
    return level if level in LEVELS else "confirm"  # unknown -> fail closed, but workable


def normalize_activation(value: Any) -> str:
    mode = str(value or "inherit").strip().lower()
    return mode if mode in ACTIVATION else "confirm"


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def is_change(tool_name: str, args: dict) -> bool:
    if tool_name in WRITE_TOOLS or tool_name == ACTIVATE_TOOL:
        return True
    if tool_name == "abap_unit_test":
        return _truthy(args.get("dangerous")) or _truthy(args.get("critical"))
    return False


def describe(tool_name: str, args: dict) -> str:
    """One line for the approval prompt."""
    target = args.get("objectUrl") or args.get("name") or "?"
    profile = f" on {args['profile']}" if args.get("profile") else ""
    transport = f" (transport {args['transport']})" if args.get("transport") else ""
    if tool_name == "abap_write":
        lines = len(str(args.get("source", "")).split("\n"))
        return f"Write {lines} lines of source to {target}{profile}{transport}"
    if tool_name == "abap_activate":
        return f"Activate {target}{profile}"
    if tool_name == "abap_lock":
        return f"Lock {target}{profile} for editing"
    if tool_name == "abap_set_text_elements":
        return f"Write {args.get('category', '?')} text elements of {target}{profile}{transport}"
    if tool_name == "abap_create_object":
        return (f"Create {args.get('objectType', '?')} {args.get('name', '?')} in "
                f"{args.get('parentName', '?')}{profile}{transport}")
    if tool_name == "abap_unit_test":
        return f"Run unit tests of {target}{profile} including dangerous/critical tests"
    return f"Run {tool_name} on {target}{profile}"


def make_pre_tool_call_hook(
    get_setting: Callable[[str, Any], Any],
    active_profile: Callable[[], Optional[str]],
) -> Callable[..., Optional[dict]]:
    def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_kwargs: Any) -> Optional[dict]:
        args = args or {}
        if not is_change(tool_name, args):
            return None

        readonly: Iterable[str] = get_setting("readonly_profiles", []) or []
        if isinstance(readonly, str):
            readonly = [p.strip() for p in readonly.split(",")]
        profile = args.get("profile") or active_profile()
        if profile and profile in set(readonly):
            return {
                "action": "block",
                "message": f'{tool_name} is blocked: profile "{profile}" is read-only '
                f"(plugins.entries.{PLUGIN}.settings.readonly_profiles).",
            }

        level = normalize_level(get_setting("safety_level", "open"))
        if tool_name == ACTIVATE_TOOL:
            activation = normalize_activation(get_setting("activation", "inherit"))
            if activation == "block":
                return {
                    "action": "block",
                    "message": "abap_activate is blocked: activation is done by a person in SAP "
                    f"(plugins.entries.{PLUGIN}.settings.activation). Tell the user the object is "
                    "saved inactive and ready to be activated.",
                }
            if activation == "confirm":
                level = "confirm"

        if level == "open":
            return None
        if level == "readonly":
            return {
                "action": "block",
                "message": f"{tool_name} is blocked: the ABAP plugin is read-only "
                f"(plugins.entries.{PLUGIN}.settings.safety_level).",
            }
        # "Always allow" in the approval prompt then covers this tool on this
        # object (and system) only, not every object.
        target = str(args.get("objectUrl") or args.get("name") or "").lower()
        rule_key = f"{PLUGIN}:{tool_name}:{profile or ''}:{target}"
        return {"action": "approve", "message": describe(tool_name, args), "rule_key": rule_key}

    return pre_tool_call
