"""Connection tools: abap_setup, abap_status, abap_profile, abap_test_connection."""

from __future__ import annotations

from .. import adt
from .. import config as cfg
from ..client import LOCKS, with_client
from ..formatters import format_profile_status
from ..models import AbapProfile, ToolInputError
from ._base import PROFILE_PROPERTY, opt_bool, opt_str, req_str, result, schema, tool_handler

# abap_setup

SETUP_SCHEMA = schema(
    "abap_setup",
    "Configure an SAP system connection profile (ADT protocol). Call this first before using any other "
    "ABAP tools. Credentials are stored in $HERMES_HOME/abap-config.json (readable only by you). The SAP "
    "system must have ADT (ABAP Development Tools) enabled.",
    {
        "name": {"type": "string", "description": "Profile name, e.g. 'dev', 'prod'. Use a short, memorable name."},
        "url": {
            "type": "string",
            "description": "ADT base URL, e.g. http://vhcalnplci.bti.local:8000 or https://sap.example.com:44300",
        },
        "username": {"type": "string", "description": "SAP logon user (e.g. DEVELOPER)"},
        "password": {"type": "string", "description": "SAP password"},
        "client": {"type": "string", "description": "SAP client / mandant, e.g. '001'"},
        "language": {"type": "string", "description": "Logon language key, e.g. 'EN'"},
        "allowUnauthorized": {
            "type": "boolean",
            "description": "Accept self-signed TLS certificates. Defaults to false. Set to true only for "
            "on-premise dev systems without a valid certificate.",
        },
    },
    ["name", "url", "username", "password"],
)


@tool_handler
def setup(args: dict) -> str:
    name = req_str(args, "name").strip()
    client = opt_str(args, "client")
    language = opt_str(args, "language")
    profile = AbapProfile(
        url=cfg.normalize_base_url(req_str(args, "url")),
        username=req_str(args, "username").strip(),
        password=req_str(args, "password"),
        client=client.strip() if client and client.strip() else None,
        language=language.strip() if language and language.strip() else None,
        allow_unauthorized=bool(opt_bool(args, "allowUnauthorized")),
    )
    cfg.save_profile(name, profile)

    # Verify right away: a wrong client or password is cheaper to find here.
    verified = False
    try:
        with with_client(cfg.resolve_config(name)):
            pass
        verification = f"Logon to {profile.url} as {profile.username} works."
        verified = True
    except Exception as exc:
        verification = f"Saved, but the logon test failed: {exc}"

    active = cfg.get_active_profile()
    state = "saved and set as active" if active == name else f'saved (active profile stays "{active}")'
    return result(
        f'SAP profile "{name}" {state}. {verification}',
        profileName=name,
        configSaved=True,
        verified=verified,
    )


# abap_status

STATUS_SCHEMA = schema(
    "abap_status",
    "Show current ABAP connection configuration status (which SAP systems are configured and which is "
    "active), plus any locks this agent is currently holding.",
    {},
)


@tool_handler
def status(args: dict) -> str:
    profiles = cfg.get_profiles()
    active = cfg.get_active_profile()
    env_profiles = [n for n in profiles if cfg.is_env_profile(n)]
    text = format_profile_status(profiles, active, env_profiles)
    held = LOCKS.all()
    if held:
        text += "\n\nLocks held by this agent:\n" + "\n".join(
            f"- {h.name} ({h.object_url}) lockHandle={h.lock_handle}" for h in held
        )
    return result(
        text,
        configured=bool(profiles),
        profileCount=len(profiles),
        activeProfile=active,
        profiles=list(profiles),
        heldLocks=[{"name": h.name, "objectUrl": h.object_url, "lockHandle": h.lock_handle} for h in held],
    )


# abap_profile

PROFILE_SCHEMA = schema(
    "abap_profile",
    "List configured SAP connection profiles, switch the active one, or delete a profile. Without "
    "arguments it lists all profiles.",
    {
        "action": {"type": "string", "description": "One of: list, use, delete. Defaults to list."},
        "name": {"type": "string", "description": "Profile name for 'use' and 'delete'."},
    },
)


@tool_handler
def profile(args: dict) -> str:
    action = (opt_str(args, "action") or "list").lower()
    name = opt_str(args, "name")
    if action == "list":
        profiles = cfg.get_profiles()
        env_profiles = [n for n in profiles if cfg.is_env_profile(n)]
        return result(
            format_profile_status(profiles, cfg.get_active_profile(), env_profiles),
            profiles=list(profiles),
            activeProfile=cfg.get_active_profile(),
        )
    if action not in ("use", "delete"):
        raise ToolInputError(f'Unknown action "{args.get("action")}". Use one of: list, use, delete.')
    if not name:
        raise ToolInputError(f'The "{action}" action requires a profile name.')
    if action == "use":
        cfg.set_active_profile(name)
        return result(f'Active SAP profile is now "{name}".', activeProfile=name)
    removed = cfg.delete_profile(name)
    active = cfg.get_active_profile()
    if removed:
        now = f'"{active}"' if active else "unset"
        text = f'Profile "{name}" deleted. Active profile is now {now}.'
    else:
        text = f'No profile named "{name}".'
    return result(text, deleted=removed, activeProfile=active)


# abap_test_connection

TEST_CONNECTION_SCHEMA = schema(
    "abap_test_connection",
    "Test the connection to an SAP system by logging in and reading the ADT compatibility graph. Use "
    "this to verify a profile works after abap_setup.",
    {"profile": PROFILE_PROPERTY},
)


@tool_handler
def test_connection(args: dict) -> str:
    config = cfg.resolve_config(opt_str(args, "profile"))
    with with_client(config) as session:
        graph = adt.compatibility_graph(session)
        discovery = adt.core_discovery(session)
    return result(
        f"Connected to {config.url} as {config.username}. Login successful.",
        url=config.url,
        username=config.username,
        client=config.client,
        language=config.language,
        compatibilityNodes=[f"{n.get('nameSpace', '')}:{n.get('name', '')}" for n in graph["nodes"]],
        discoveryTitles=[d["title"] for d in discovery],
    )
