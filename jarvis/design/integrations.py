"""External design tool integrations (v1.2).

The plan allows integrating with external design tools, but requires every
external integration to be **permissioned**. So the registry is explicit:

* Nothing is enabled by default. Local generation is the default backend.
* An integration must be both *configured* (credentials present) and *enabled*
  (explicitly switched on) before it is usable.
* :func:`describe_integrations` reports the state of each, so JARVIS can say
  whether an artifact came from an external tool or was generated locally.

No integration is implemented yet — the registry exists so that adding one does
not require touching the tools, and so an unavailable integration is reported
rather than silently substituted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("design.integrations")


@dataclass
class DesignIntegration:
    """A possible external design tool."""

    name: str
    display: str
    #: Env var that must be set for the integration to be configurable.
    credential_setting: str = ""
    #: Whether the user has switched it on.
    enabled_setting: str = ""

    def configured(self, settings) -> bool:
        if not self.credential_setting:
            return False
        return bool(getattr(settings, self.credential_setting, ""))

    def enabled(self, settings) -> bool:
        if not self.enabled_setting:
            return False
        return bool(getattr(settings, self.enabled_setting, False))

    def usable(self, settings) -> bool:
        return self.configured(settings) and self.enabled(settings)

    def describe(self, settings=None) -> Dict[str, Any]:
        from ..config import get_settings

        settings = settings or get_settings()
        return {
            "name": self.name,
            "display": self.display,
            "configured": self.configured(settings),
            "enabled": self.enabled(settings),
            "usable": self.usable(settings),
            "implemented": False,
        }


#: Known integrations. Credentials are opt-in; none are on by default.
INTEGRATIONS: List[DesignIntegration] = [
    DesignIntegration(
        name="figma",
        display="Figma",
        credential_setting="design_figma_token",
        enabled_setting="design_figma_enabled",
    ),
    DesignIntegration(
        name="mermaid_cli",
        display="Mermaid CLI",
        credential_setting="design_mermaid_path",
        enabled_setting="design_mermaid_enabled",
    ),
]


def get_integration(name: str) -> Optional[DesignIntegration]:
    for integration in INTEGRATIONS:
        if integration.name == name:
            return integration
    return None


def usable_integrations(settings=None) -> List[DesignIntegration]:
    from ..config import get_settings

    settings = settings or get_settings()
    return [i for i in INTEGRATIONS if i.usable(settings)]


def describe_integrations(settings=None) -> Dict[str, Any]:
    from ..config import get_settings

    settings = settings or get_settings()
    return {
        "integrations": [i.describe(settings) for i in INTEGRATIONS],
        "default_backend": "local",
        "policy": (
            "An integration must be configured AND enabled before use. Nothing is "
            "enabled by default; artifacts are generated locally and labelled as such."
        ),
    }
