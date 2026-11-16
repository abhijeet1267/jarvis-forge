"""Permission engine (sections 8, 10, 26, 48).

Turns a planned action into one of three outcomes:

* ``EXECUTE`` -- run it now
* ``CONFIRM`` -- create a pending confirmation and wait for the user
* ``BLOCK``   -- refuse, with a reason

The engine is the *only* component allowed to authorise execution. Agents call
tools; they never bypass this layer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.models import (
    ConfirmationState,
    PolicyDecision,
    PlannedAction,
    RiskLevel,
    ToolSpec,
    new_id,
    new_secret_id,
    utcnow,
)
from ..db.database import get_db
from ..logging_setup import get_logger
from ..registry.devices import DeviceRegistry
from ..registry.tools import ToolRegistry
from .risk import RiskClassifier

log = get_logger("security.policy")


class PolicyResult:
    """Outcome of evaluating one action."""

    def __init__(
        self,
        decision: PolicyDecision,
        reason: str,
        confirmation_id: Optional[str] = None,
        preview: Optional[str] = None,
    ) -> None:
        self.decision = decision
        self.reason = reason
        self.confirmation_id = confirmation_id
        self.preview = preview

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PolicyResult({self.decision.value}, {self.reason!r})"


class PermissionEngine:
    """Evaluates actions against device capability and risk policy."""

    def __init__(
        self,
        tools: ToolRegistry,
        devices: DeviceRegistry,
        classifier: Optional[RiskClassifier] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.tools = tools
        self.devices = devices
        self.classifier = classifier or RiskClassifier()
        self.settings = settings or get_settings()
        self.db = get_db()
        
        # Add provenance columns (Phase 71 migration)
        self._ensure_provenance_columns()

    # -- explicit grants -----------------------------------------------------
    def grant(self, tool: str, device: Optional[str] = None, subject: str = "user") -> None:
        # Stored canonically, so new rows use today's name; `_latest_permission` still
        # matches the older spellings, so a row written before a rename keeps binding.
        tool = self.tools.canonical(tool)
        self.db.execute(
            """
            INSERT INTO permissions (permission_id, subject, tool, device, granted)
            VALUES (?, ?, ?, ?, 1)
            """,
            (new_id("PERM"), subject, tool, device),
        )

    def _ensure_provenance_columns(self):
        """Add provenance columns to confirmations table (Phase 71 migration)."""
        try:
            self.db.query("SELECT requested_by_session FROM confirmations LIMIT 1")
            # Columns already exist
            return
        except Exception:
            pass
        
        # Run migrations
        for col_name, col_def in [
            ("requested_by_session", "TEXT DEFAULT ''"), 
            ("approved_by_session", "TEXT DEFAULT ''"),
            ("requested_surface", "TEXT DEFAULT ''"),
            ("approved_surface", "TEXT DEFAULT ''"),
        ]:
            try:
                self.db.execute(f"ALTER TABLE confirmations ADD COLUMN {col_name} {col_def}")
                log.info(f"provenance column added: {col_name}")
            except Exception as e:
                # Column might already exist from previous run
                log.info(f"provenance column migration skip: {col_name}", extra_fields={"error": str(e)})
        
        log.info("confirmations provenance schema ready")


    def revoke(self, tool: str, device: Optional[str] = None, subject: str = "user") -> None:
        """Revoke a tool. Records an explicit denial even if none existed."""
        tool = self.tools.canonical(tool)
        self.db.execute(
            """
            INSERT INTO permissions (permission_id, subject, tool, device, granted, revoked_at)
            VALUES (?, ?, ?, ?, 0, datetime('now'))
            """,
            (new_id("PERM"), subject, tool, device),
        )
        log.info("permission revoked", extra_fields={"tool": tool, "device": device})

    def is_granted(self, tool: str, device: Optional[str] = None, subject: str = "user") -> bool:
        row = self._latest_permission(tool, device, subject)
        return bool(row and row["granted"])

    def _latest_permission(
        self, tool: str, device: Optional[str], subject: str
    ) -> Optional[Dict[str, Any]]:
        """The permission row that currently speaks for this tool and device.

        Matched over every spelling of the tool, because the name is not the capability:
        a revocation written as ``open_app`` must still bind once the tool answers to
        ``open_application``. Matching the requested name alone would silently lift a
        standing block during a rename, which is a privilege escalation with no error.

        Ordered by ``rowid``, never by ``granted_at``. That column defaults to
        ``datetime('now')``, which has one-second granularity, so a grant and
        its revocation issued in the same second tie; ``ORDER BY granted_at DESC
        LIMIT 1`` then returned whichever row the index happened to yield first,
        which was the grant -- so revoking a standing permission silently did
        nothing, permanently. Insertion order is monotonic and is the order the
        user acted in, so the newest statement always wins.
        """
        spellings = self.tools.spellings(tool)
        # A row that names no device is read differently in each direction, and the
        # asymmetry is the point. A NULL *grant* stays inert: promoting it to a wildcard
        # would hand every device an authority the user granted against no device at all,
        # which is exactly the scope decision Phase 10 declined to make silently.
        #
        # A NULL *denial* binds everywhere. `revoke(tool)` with no device is the only form
        # most callers would write, and under the old lookup it revoked nothing for any
        # real action -- measured: `revoke("open_app")` then evaluating `open_app` on
        # laptop_main answered EXECUTE, reason "low-risk action". No product surface calls
        # this method today (measured: no route, tool or dashboard control reaches
        # PolicyStore.revoke; only device revocation is wired), so this is a latent trap
        # being closed, not a live bug being reported.
        rows = self.db.query(
            f"""
            SELECT granted, device, granted_at, revoked_at FROM permissions
            WHERE subject = ? AND tool IN ({", ".join("?" * len(spellings))})
              AND (device = ? OR (device IS NULL AND granted = 0))
            ORDER BY rowid DESC LIMIT 1
            """,
            (subject, *spellings, device),
        )
        return rows[0] if rows else None

    def _is_revoked(self, tool: str, device: str) -> bool:
        row = self._latest_permission(tool, device, "user")
        return bool(row and not row["granted"])

    def is_revoked(self, tool: str, device: Optional[str] = None,
                   subject: str = "user") -> bool:
        """Public read of the same rule, for the surface that has to show it.

        Phase 54 exposed revocation as a product action, and an action with no readable result
        is one a user cannot check. This is a read of ``_latest_permission``, not a second
        opinion about what a denial means.
        """
        return self._is_revoked(tool, device)

    # -- evaluation ----------------------------------------------------------
    def evaluate(self, action: PlannedAction, session_id: str = "", request_id: str = "",
                requested_surface: str = "") -> PolicyResult:
        """Evaluate a single planned action.

        ``requested_surface`` is which surface raised the request. It is recorded
        on the confirmation so that a later answer can be compared with it --
        a narrow surface approving its own request is refused, and that rule
        needs to know the request came from a phone.
        """
        spec = self.tools.get(action.tool)
        if spec is None:
            return PolicyResult(PolicyDecision.BLOCK, f"unknown tool {action.tool!r}")

        # 1. Parameter validity -- never invent parameters.
        problems = self.tools.validate_params(action.tool, action.params)
        if problems:
            return PolicyResult(PolicyDecision.BLOCK, "; ".join(problems))

        # 2. Device must exist and be allowed for this tool.
        device = self.devices.get(action.device)
        if device is None:
            return PolicyResult(PolicyDecision.BLOCK, f"unknown device {action.device!r}")
        if spec.allowed_devices and action.device not in spec.allowed_devices:
            return PolicyResult(
                PolicyDecision.BLOCK,
                f"{action.tool} is not permitted on device {action.device}",
            )
        if not device.online:
            return PolicyResult(
                PolicyDecision.BLOCK, f"device {action.device} is offline"
            )

        # 2b. Filesystem guard -- protected paths are refused, never merely
        #     confirmed, so the user is never nudged into a dangerous approval.
        refusal = self._path_guard(action)
        if refusal:
            return PolicyResult(PolicyDecision.BLOCK, refusal)

        # 2c. Home actuation guard -- a device the sentence did not identify is
        #     refused for the same reason, before any approval is requested.
        refusal = self._home_guard(action)
        if refusal:
            return PolicyResult(PolicyDecision.BLOCK, refusal)

        # 3. A revoked explicit grant blocks the tool outright.
        if self._is_revoked(action.tool, action.device):
            return PolicyResult(
                PolicyDecision.BLOCK, f"permission for {action.tool} has been revoked"
            )

        # 4. Risk policy.
        decision, reason = self.classifier.decide(spec, action.params)

        # 4b. The autonomy dial. It sits after the classifier and can only make
        #     the verdict stricter, never looser -- so a low dial asks more, and
        #     a high dial still cannot auto-execute a destructive action (the
        #     ceiling is inside `AutonomyControl.apply`). A dial failure must not
        #     widen anything, so a broken dial leaves the classifier's verdict.
        try:
            from .autonomy import get_autonomy

            decision, reason = get_autonomy().apply(spec, action.params, decision, reason)
        except Exception as exc:  # pragma: no cover - defensive
            log.info("autonomy dial unavailable", extra_fields={"error": str(exc)})

        # 5. An explicit standing grant downgrades CONFIRM to EXECUTE for
        #    reversible actions the user has already approved once -- but never
        #    for actions that establish or change trust, and never when the
        #    target device has not been paired. Approving a standing grant is
        #    consent to repeat an action, not consent to trust a new device.
        if (
            decision is PolicyDecision.CONFIRM
            and spec.reversible
            and self.is_granted(action.tool, action.device)
            and not self._trust_sensitive(action)
        ):
            decision, reason = PolicyDecision.EXECUTE, "covered by a standing grant"

        if decision is PolicyDecision.CONFIRM:
            confirmation_id = self._create_confirmation(
                spec, action, session_id=session_id, request_id=request_id,
                requested_surface=requested_surface,
            )
            return PolicyResult(
                decision,
                reason,
                confirmation_id=confirmation_id,
                preview=self._preview(spec, action),
            )

        return PolicyResult(decision, reason)

    def pre_execute_check(self, action: PlannedAction) -> Optional[str]:
        """Re-run the refusal gates immediately before an action executes.

        A confirmation is minted against conditions observed when it was created
        and may sit unanswered for up to ``confirm_timeout``. Without this check
        the approval acted as a permanent bypass: Phase 9 measured that
        revoking ``delete_file`` after its confirmation existed still executed
        the deletion, because ``resolve_confirmation`` rebuilt the action from
        the stored record and went straight to ``agent.execute()``.

        Only the *refusal* gates are re-run -- parameter validity, device
        existence/permission/reachability, the filesystem guard and revocation.
        The risk classifier is deliberately not re-run: its CONFIRM verdict is
        what the user already answered, so re-asking would loop.
        """
        spec = self.tools.get(action.tool)
        if spec is None:
            return f"unknown tool {action.tool!r}"

        problems = self.tools.validate_params(action.tool, action.params)
        if problems:
            return "; ".join(problems)

        device = self.devices.get(action.device)
        if device is None:
            return f"unknown device {action.device!r}"
        if spec.allowed_devices and action.device not in spec.allowed_devices:
            return f"{action.tool} is not permitted on device {action.device}"
        if not device.online:
            return f"device {action.device} is offline"

        refusal = self._path_guard(action)
        if refusal:
            return refusal

        refusal = self._home_guard(action)
        if refusal:
            return refusal

        if self._is_revoked(action.tool, action.device):
            return f"permission for {action.tool} has been revoked"

        return None

    def _trust_sensitive(self, action: PlannedAction) -> bool:
        """True when an action must always be confirmed, grant or not.

        Two cases (v0.6):

        * **Actions that establish or destroy trust.** ``approve_pairing`` and
          ``remove_phone`` change who is allowed to talk to this machine. A
          standing grant on a previous transfer must never silently authorise
          them.
        * **Transfers to a device that is not paired.** Sending a file to an
          unpaired device must always surface a confirmation, even if the user
          previously approved a transfer to a *different*, paired device.
        """
        if action.tool in {"approve_pairing", "remove_phone", "register_phone"}:
            return True

        if action.tool == "send_to_device":
            target = action.params.get("target_device")
            if not target:
                return True
            device = self.devices.get(str(target))
            if device is None:
                # Fall back to a name match, mirroring the tool's resolution.
                wanted = str(target).strip().lower()
                for candidate in self.devices.all():
                    if candidate.device_id.lower() == wanted or str(
                        candidate.metadata.get("display_name", "")
                    ).lower() == wanted:
                        device = candidate
                        break
            if device is None or not device.is_paired:
                return True

        return False

    def _home_guard(self, action: PlannedAction) -> Optional[str]:
        """Refuse a home actuation whose device the sentence did not identify.

        The path guard has this rule for files, for the same reason: a confirmation is
        a question about one specific thing, so asking "switch on Study AC?" on the
        strength of the word "heater" gathers consent against a device nobody named.
        Refusing here rather than in the tool is also what makes it hold for a schedule,
        which re-evaluates through this engine and never passes through a typed sentence.
        """
        spec = self.tools.get(action.tool)
        if spec is None or spec.agent != "home" or "device" not in action.params:
            return None
        word = str(action.params.get("device") or "").strip()
        if not word:
            return None

        from ..home.gateway import get_home_gateway

        gateway = get_home_gateway()
        target, described = gateway.actuation_target(word)
        if target is not None:
            return None
        if described:
            return (
                f"Which {word} did you mean -- "
                + ", ".join(d.name for d in described)
                + "? I will not switch one of them on a guess."
            )
        return (
            f"There is no home device matching {word!r}. The gateway has: "
            + ", ".join(d.name for d in gateway.devices[:8])
            + ("…" if len(gateway.devices) > 8 else "")
            + "."
        )

    def _path_guard(self, action: PlannedAction) -> Optional[str]:
        """Refuse file operations aimed at protected system locations.

        Every string *value* in the parameters is examined, at any depth,
        because the argument's name is chosen by the planner and is not a
        security boundary. Checking only params literally called ``path`` left
        ``search_files(directory=...)`` and ``args=[protected_path]`` walking
        straight past a guard the docs described as absolute.
        """
        from .paths import iter_string_values, protection_reason

        for value in iter_string_values(action.params):
            reason = protection_reason(value)
            if reason:
                return reason
        return None

    # -- confirmations -------------------------------------------------------
    def _create_confirmation(
        self,
        spec: ToolSpec,
        action: PlannedAction,
        session_id: str,
        request_id: str,
        requested_surface: str = "",
    ) -> str:
        confirmation_id = new_secret_id("CONF")
        
        self.db.execute(
            """
            INSERT INTO confirmations
                (confirmation_id, request_id, session_id, tool, params, device,
                 risk, preview, state, created_at, requested_by_session, requested_surface)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                confirmation_id,
                request_id,
                session_id,
                action.tool,
                json.dumps(action.params),
                action.device,
                int(spec.risk),
                self._preview(spec, action),
                utcnow().isoformat(),
                session_id,
                # The surface that raised the request, as the caller knows
                # it. It used to be guessed from the request id --
                # `"phone" if "/" in request_id` -- and a request id is
                # `REQ-<hex>`, so the guess could never be "phone" and every
                # confirmation was recorded as "dashboard" even when a phone
                # had raised it. The value had no readers, so nothing broke;
                # it was simply a fact that was wrong and unused.
                requested_surface or "unknown",
            ),
        )
        log.info(
            "confirmation requested",
            extra_fields={"confirmation_id": confirmation_id, "tool": action.tool},
        )
        return confirmation_id

    def pending(self) -> List[Dict[str, Any]]:
        """Pending confirmations that have not expired."""
        self.expire_stale()
        rows = self.db.query(
            "SELECT * FROM confirmations WHERE state = ? ORDER BY created_at DESC",
            (ConfirmationState.PENDING.value,),
        )
        return [self._row_to_confirmation(r) for r in rows]

    def get_confirmation(self, confirmation_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM confirmations WHERE confirmation_id = ?", (confirmation_id,)
        )
        return self._row_to_confirmation(rows[0]) if rows else None

    # -- expiry --------------------------------------------------------------
    def _expiry_seconds(self) -> int:
        return max(1, int(self.settings.confirm_timeout))

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def is_expired(self, record: Dict[str, Any]) -> bool:
        """True when a pending confirmation has outlived its window.

        An approval that sits unanswered forever is a standing authorisation
        nobody is watching, so pending requests must go stale.
        """
        created = self._parse_timestamp(record.get("created_at"))
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (utcnow() - created).total_seconds() > self._expiry_seconds()

    def expire_stale(self) -> int:
        """Mark every expired pending confirmation as EXPIRED. Returns the count."""
        rows = self.db.query(
            "SELECT confirmation_id, created_at FROM confirmations WHERE state = ?",
            (ConfirmationState.PENDING.value,),
        )
        expired = [
            row["confirmation_id"]
            for row in rows
            if self.is_expired({"created_at": row["created_at"]})
        ]
        for confirmation_id in expired:
            self.db.execute(
                "UPDATE confirmations SET state = ?, resolved_at = ? WHERE confirmation_id = ?",
                (ConfirmationState.EXPIRED.value, utcnow().isoformat(), confirmation_id),
            )
        if expired:
            log.info("confirmations expired", extra_fields={"count": len(expired)})
        return len(expired)

    def resolve(self, confirmation_id: str, approve: bool, 
                approver_session: str = "", approver_surface: str = "") -> Optional[Dict[str, Any]]:
        record = self.get_confirmation(confirmation_id)
        if record is None or record["state"] != ConfirmationState.PENDING.value:
            return record

        # Refuse to honour an approval that took too long to arrive.
        if self.is_expired(record):
            self.db.execute(
                "UPDATE confirmations SET state = ?, resolved_at = ? WHERE confirmation_id = ?",
                (ConfirmationState.EXPIRED.value, utcnow().isoformat(), confirmation_id),
            )
            record["state"] = ConfirmationState.EXPIRED.value
            log.info("confirmation expired before approval", extra_fields={"id": confirmation_id})
            return record
        
        # Self-approval boundary: a device cannot approve its own requests.
        # The check needs a real session identity on both sides: an empty approver
        # session (a direct laptop/CLI approval with no session token) has no "who" to
        # compare, and comparing "" == "" would deny the ordinary confirmation lifecycle.
        # The surface-aware rule (a phone may not approve itself, a dashboard may) is
        # enforced in the orchestrator; this is the engine-level fallback for raw calls.
        requested_session = record.get("requested_by_session", "")
        if approver_session and requested_session == approver_session:
            log.warning(
                "self-approval rejected",
                extra_fields={
                    "confirmation_id": confirmation_id,
                    "session": approver_session,
                },
            )
            record["state"] = ConfirmationState.DENIED.value
            self.db.execute(
                "UPDATE confirmations SET state = ?, resolved_at = ? WHERE confirmation_id = ?",
                (ConfirmationState.DENIED.value, utcnow().isoformat(), confirmation_id),
            )
            return record
        
        # Cross-surface check: phone requesting, dashboard approving - allowed
        # Phone approving phone request - blocked above
        # Dashboard approving dashboard request - allowed for same-session (Phase 58 rule)
        
        # Update provenance tracking
        self.db.execute(
            """UPDATE confirmations 
               SET state = ?, resolved_at = ?, approved_by_session = ?, approved_surface = ?
               WHERE confirmation_id = ?""",
            (
                ConfirmationState.APPROVED.value if approve else ConfirmationState.DENIED.value,
                utcnow().isoformat(),
                approver_session if approver_session else record["session_id"],
                approver_surface if approver_surface else "unknown",
                confirmation_id,
            ),
        )

        record["state"] = ConfirmationState.APPROVED.value if approve else ConfirmationState.DENIED.value
        log.info(
            "confirmation resolved",
            extra_fields={"confirmation_id": confirmation_id, "state": record["state"]},
        )
        return record

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _preview(spec: ToolSpec, action: PlannedAction) -> str:
        """Human-readable summary shown before a confirmation.

        A home actuation is phrased by the home module, which is the only thing that
        knows what the words resolve to: the user approves "switch on Living room fan
        (living.fan)", not "device=living room fan". Everything else lists its
        parameters, which is as specific as it can be made without a resolver.
        """
        if spec.agent == "home" and "device" in action.params:
            from ..tools.home import describe_actuation

            return describe_actuation(
                str(action.params.get("device") or ""),
                str(action.params.get("action") or ""),
                action.params.get("value"),
            )
        if spec.name == "run_coding_agent":
            # What the user is approving is the instruction and where it runs, not a
            # dict. A coding agent handed a parameter dump to read on a phone screen
            # is a confirmation nobody can actually make.
            asked = str(action.params.get("prompt") or "").strip()
            where = str(action.params.get("path") or "").strip()
            said = f'ask the coding agent "{asked}"' if asked else "ask the coding agent"
            if action.params.get("resume"):
                said += " (continuing its last session)"
            if where:
                leaf = where.rstrip("/").split("/")[-1] or where
                said += f" in {leaf}"
            return said
        bits = [f"{spec.name} on {action.device}"]
        for key, value in action.params.items():
            bits.append(f"{key}={value}")
        return " | ".join(bits)

    @staticmethod
    def _row_to_confirmation(row: Any) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["params"] = json.loads(item.get("params") or "{}")
        except json.JSONDecodeError:
            item["params"] = {}
        item["risk"] = RiskLevel(item.get("risk", 0))
        return item
