# hermes-abap-remote-fs

ABAP remote filesystem plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

With it, Hermes can read, search, write and activate ABAP objects on SAP systems. It talks to the system through the ADT protocol (ABAP Development Tools), the same REST interface Eclipse uses. That means the normal ICM port of the AS ABAP instance, not an RFC port.

This is the Hermes port of [pi-abap-fs](https://github.com/Smotherer007/pi-abap-fs). The 27 tools, their parameters and the config file format are the same. pi-abap-fs builds on the npm library [abap-adt-api](https://github.com/marcellourbani/abap-adt-api). This plugin has its own ADT client in plain Python, ported from that library's request and response code, and needs nothing beyond the standard library.

## Installation

```bash
hermes plugins install Smotherer007/hermes-abap-remote-fs
hermes plugins enable abap-remote-fs
```

For development, install from a local checkout:

```bash
hermes plugins install file://$PWD/hermes-abap-remote-fs --enable
```

## Quick start

```yaml
abap_setup:
  name: dev
  url: http://vhcalnplci.bti.local:8000
  username: DEVELOPER
  password: ...
  client: "001"
```

`abap_setup` tries to log on right away, so a wrong password or client shows up immediately. The URL needs its scheme and port: `http://host:8000+<sysnr>` or `https://host:44300+<sysnr>`. A pasted `/sap/bc/adt` suffix is removed.

The usual flow is `abap_search` → `abap_read` → `abap_write` → `abap_syntax_check` → `abap_activate`.

## Tools

All tools belong to the `abap` toolset.

| Group | Tools |
|---|---|
| Connection | `abap_setup`, `abap_status`, `abap_profile`, `abap_test_connection` |
| Search & read | `abap_search`, `abap_object_types`, `abap_node_contents`, `abap_object_structure`, `abap_read`, `abap_where_used`, `abap_text_elements` |
| Modify | `abap_lock`, `abap_unlock`, `abap_write`, `abap_activate`, `abap_create_object`, `abap_set_text_elements` |
| Check & test | `abap_syntax_check`, `abap_unit_test`, `abap_atc` |
| Data | `abap_query`, `abap_table` |
| Operations & transport | `abap_transports`, `abap_transport_details`, `abap_dumps`, `abap_traces`, `abap_trace_hitlist` |

Each tool returns JSON. `result` holds the text for the model, and the other keys hold structured details. On failure the tool returns `{"error": "..."}`, and the text is SAP's own message.

## Locks: what is different from pi-abap-fs

ADT locks belong to the *stateful session* that took them. When that session ends, the lock is gone. pi-abap-fs opens a fresh session for every tool call and drops it at the end. So the handle that `abap_lock` returns no longer protects anything by the time `abap_write` runs.

Here, `abap_lock` keeps its session open. As long as the lock is held, every later call on the same object runs inside that session: `abap_write`, `abap_set_text_elements`, `abap_activate` and `abap_unlock`. It does not matter whether the call passes the handle, the object URL or just the name. `abap_status` lists the locks the agent is holding.

A lock is released by `abap_unlock`, or after 15 minutes without use, or when the Hermes process exits. If SAP ends the session first, for example through a timeout or a restart, the next call reports that the lock is gone.

Without `abap_lock`, nothing changes: `abap_write` and `abap_set_text_elements` lock, write and unlock inside a single call, like pi-abap-fs.

## Guard rails

Three settings under `plugins.entries.abap-remote-fs.settings` control changes to the system. They also appear as a form in the Desktop app under **Capabilities → Plugins** and apply from the next tool call on.

| Setting | Values | Applies to |
|---|---|---|
| `safety_level` | `open` (default), `confirm`, `readonly` | lock, write, text elements, object creation, and unit tests with `dangerous`/`critical` |
| `activation` | `inherit` (default), `confirm`, `block` | `abap_activate` |
| `readonly_profiles` | list of profile names | everything above, for these systems |

- `confirm` sends the call through Hermes' approval gate. In a chat that means the usual approval prompt. **Always allow** in that prompt only covers that tool on that object.
- `activation: block` means a person activates in SAP. The model is told that the object is saved inactive and ready.
- `readonly_profiles` wins over everything else. Use it for production (`[prod]`). A call without a `profile` argument counts as the active profile.

```bash
hermes config set plugins.entries.abap-remote-fs.settings.activation block
hermes config set plugins.entries.abap-remote-fs.settings.readonly_profiles '["prod"]'
```

Reads are never affected: search, read, where-used, syntax check, ATC, dumps, traces and data preview. ADT's data preview only runs SELECTs.

## Configuration

A system can be configured in any of three ways. All of them can be combined.

1. **`abap_setup`** writes `$HERMES_HOME/abap-config.json`, atomically and with mode `0600`. Every Hermes profile has its own `HERMES_HOME` and therefore its own systems. `ABAP_CONFIG` points the plugin at a different file.
2. **The file itself.** It has the same format as pi-abap-fs, so `cp ~/.pi/abap-config.json ~/.hermes/` is enough.

   ```json
   {
     "activeProfile": "dev",
     "profiles": {
       "dev": { "url": "http://172.31.1.12:8000", "username": "NEO", "password": "…", "client": "900" }
     }
   }
   ```

   Add `"allowUnauthorized": true` for a self-signed certificate. Unlike pi-abap-fs, this only affects that profile's requests.
3. **Environment variables**, for containers: `ABAP_URL`, `ABAP_USER`, `ABAP_PASSWORD`, and optionally `ABAP_CLIENT`, `ABAP_LANGUAGE`, `ABAP_ALLOW_UNAUTHORIZED` and `ABAP_PROFILE_NAME` (default `default`). This profile lives only in memory and is never written to the file. If the file has a profile with the same name, the file wins.

The plugin rereads the file when it changes, so a running gateway sees profiles added from the CLI.

The SAP user needs a role with ADT authorization (`S_DEVELOP` and similar). The plugin can do exactly what that user can do.

## Limitations

- `abap_create_object` cannot create packages (`DEVC/K`). ADT needs a software component and a transport layer for that, and the tool does not ask for them. pi-abap-fs has the same limit.
- Only basic authentication. SAML, X.509 and OAuth are not supported.

## Development

```bash
python -m pip install pytest pyyaml
python -m pytest tests

hermes plugins doctor . --ci
hermes plugins validate .
```

The tests import the plugin the way Hermes does and talk to a small SAP stand-in on localhost. It behaves like the parts of an ADT system the client depends on: basic auth, the CSRF token, session cookies, stateful contexts, and locks that end with their session. The XML fixtures for traces and data preview come from abap-adt-api's own tests. The others are modelled on real ADT responses. **None of this replaces a first run against a real system**, especially for ATC, unit tests and text elements.

| Path | Content |
|---|---|
| `adt_http.py` | ADT session: basic auth, cookies, CSRF, stateless/stateful |
| `adt.py` | one function for each abap-adt-api call that pi-abap-fs uses |
| `xmlutil.py` | parses XML into the shape abap-adt-api's parsers expect |
| `client.py` | session runner, object resolution, lock registry |
| `formatters.py` | pure display functions |
| `config.py` | profile store |
| `tools/` | the 27 tools, grouped like the table above |
| `safety.py` | guard rails (`pre_tool_call` hook) |

## License

MIT
