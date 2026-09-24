"""Data types for the Hermes ABAP remote filesystem plugin.

The ADT payloads themselves stay plain dicts shaped like abap-adt-api's
results (``"adtcore:name"``, ``"tm:number"``, ...), so the formatters can
follow pi-abap-fs line by line. Only the configuration and the errors are
typed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class AbapProfile:
    """One SAP system connection."""

    #: ADT base URL, e.g. http://vhcalnplci.bti.local:8000
    url: str
    #: SAP logon user
    username: str
    #: Password (stored with 0600 permissions, or taken from the environment)
    password: str
    #: SAP client / mandant, e.g. "001"
    client: Optional[str] = None
    #: Logon language key, e.g. "EN"
    language: Optional[str] = None
    #: Accept self-signed TLS certificates (on-premise dev systems)
    allow_unauthorized: bool = False

    def to_json(self) -> dict:
        """Same shape as pi-abap-fs writes, so one file serves both agents."""
        data: dict = {"url": self.url, "username": self.username, "password": self.password}
        if self.client is not None:
            data["client"] = self.client
        if self.language is not None:
            data["language"] = self.language
        if self.allow_unauthorized:
            data["allowUnauthorized"] = True
        return data

    @staticmethod
    def from_json(raw: dict) -> "AbapProfile":
        client = raw.get("client")
        language = raw.get("language")
        return AbapProfile(
            url=str(raw.get("url", "")),
            username=str(raw.get("username", "")),
            password=str(raw.get("password", "")),
            client=str(client) if client not in (None, "") else None,
            language=str(language) if language not in (None, "") else None,
            allow_unauthorized=bool(raw.get("allowUnauthorized", raw.get("allow_unauthorized", False))),
        )


# Errors


class AbapNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__("No SAP connection configured. Use the abap_setup tool first.")


class AbapObjectNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f'No ABAP object found matching "{name}".')


class AbapProfileNotFoundError(Exception):
    def __init__(self, name: str, available) -> None:
        super().__init__(f'Profile "{name}" not found. Available: {", ".join(available) or "none"}')


class AdtError(Exception):
    """An error reported by the SAP system or the HTTP layer.

    ``message`` is what SAP said, so the model sees the SAP text directly.
    """

    def __init__(
        self,
        message: str,
        status: int = 0,
        kind: str = "",
        properties: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.properties = properties or {}

    @property
    def is_login_error(self) -> bool:
        return self.kind == "csrf" or self.status == 401


class ToolInputError(ValueError):
    """Invalid arguments from the model. Reported back as a tool error."""
