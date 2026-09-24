"""Hermes ABAP remote filesystem plugin.

Read, search, write and activate ABAP objects on SAP systems through the ADT
protocol (ABAP Development Tools). Port of the pi extension pi-abap-fs: the
same 27 tools with the same parameters, the same config file format, and a
pure-Python ADT client in place of the npm library abap-adt-api.

Layout:
  models.py      profile dataclass and error types
  xmlutil.py     minidom -> the object shape abap-adt-api's parsers expect
  adt_http.py    ADT session: basic auth, cookies, CSRF, stateless/stateful
  adt.py         the ADT operations, one per abap-adt-api function used
  client.py      session runner, object resolution, lock registry
  formatters.py  pure functions: ADT data -> display strings
  config.py      profile store, persisted with 0600 permissions
  tools/         the 27 tools, grouped by area
  safety.py      safety level, activation rule, read-only profiles
"""

from __future__ import annotations

import atexit
import logging
from typing import Any, Mapping

from . import config as cfg
from .client import LOCKS
from .safety import make_pre_tool_call_hook
from .tools import TOOLS, TOOLSET

logger = logging.getLogger(__name__)

_atexit_registered = False


def _prompt_section(_session: Mapping[str, Any]) -> str:
    """Short, frozen-per-session pointer to the tools and the configured systems. No passwords."""
    lines = [
        "SAP ABAP tools (abap_*) work on SAP systems through ADT. Usual flow: abap_search -> abap_read -> "
        "abap_write (full source) -> abap_syntax_check -> abap_activate. Writes need a transport unless the "
        "object is local ($TMP); find one with abap_transports.",
    ]
    try:
        profiles = cfg.get_profiles()
        active = cfg.get_active_profile()
    except Exception:
        profiles, active = {}, None
    if profiles:
        lines.append("Configured SAP systems (profile parameter):")
        for name, profile in profiles.items():
            marker = " (active)" if name == active else ""
            client = f", client {profile.client}" if profile.client else ""
            lines.append(f"- {name}{marker}: {profile.username}@{profile.url}{client}")
    else:
        lines.append("No SAP system is configured yet; abap_setup adds one.")
    return "\n".join(lines)


def register(ctx) -> None:
    """Called once by the Hermes plugin loader."""
    global _atexit_registered
    cfg.load_config()

    for tool_schema, handler in TOOLS:
        ctx.register_tool(
            name=tool_schema["name"],
            toolset=TOOLSET,
            schema=tool_schema,
            handler=handler,
            description=tool_schema["description"],
        )

    def setting(key: str, default: Any) -> Any:
        try:
            return ctx.get_config(key, default=default)
        except Exception:
            return default

    ctx.register_hook("pre_tool_call", make_pre_tool_call_hook(setting, cfg.get_active_profile))

    register_section = getattr(ctx, "register_system_prompt_section", None)
    if callable(register_section):
        try:
            register_section("abap-remote-fs.overview", _prompt_section)
        except Exception:
            logger.debug("abap-remote-fs: could not register the system prompt section", exc_info=True)

    # Locks live in open SAP sessions. Release them when the process ends
    # instead of leaving them to the server-side session timeout.
    if not _atexit_registered:
        atexit.register(LOCKS.release_all)
        _atexit_registered = True
