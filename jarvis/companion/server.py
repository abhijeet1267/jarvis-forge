"""Run the companion gateway as a managed sibling process of the backend.

The gateway is a *separate* process (own event loop, own TLS cert, own port
— see :mod:`jarvis.companion.gateway`) because it binds ``0.0.0.0`` and must
not take the main backend down with it. This module owns its lifecycle so
that "start the backend" reliably means "start the backend *and*, when LAN
mode is on, the gateway" and "stop the backend" reliably tears both down —
instead of the gateway being a forgotten ``uv run`` left holding port 8766.

Security notes:
- The readiness probe verifies the gateway's TLS certificate against the
  exact file we generated (``verify=<cert>``); it never disables verification.
- The child runs in its own process group; ``stop()`` signals the group so a
  uvicorn reload worker cannot outlive the request to quit.
- Certificate generation failures are fatal to *start* (a gateway without a
  valid cert would serve traffic it cannot authenticate), and are reported,
  not swallowed.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from ..config import Settings, get_settings
from .cert import CompanionCertError, ensure

log = logging.getLogger("jarvis.companion.server")

HEALTH_PATH = "/companion/health"
STARTUP_TIMEOUT_SECONDS = 15.0
_STOP_GRACE_SECONDS = 10.0


def _verified_context(cert: Path) -> ssl.SSLContext:
    """A client TLS context that verifies the chain against *our own* cert.

    The gateway's certificate is self-signed and its subject is the laptop's
    host name, not the address a client dials — so a naive ``verify=cert``
    (which also checks the host) refuses a perfectly legitimate connection.
    The correct check for a self-signed LAN endpoint is chain verification
    against the exact certificate we generated: a MITM presenting any
    *other* certificate still fails, but the host name is not required to
    match. The Android client performs the equivalent fingerprint pin
    (roadmap R1.4); this is the same guarantee, expressed as a CA check.
    Verification is never disabled — ``CERT_REQUIRED`` stays on.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(str(cert))
    return ctx


@dataclass
class GatewayStatus:
    running: bool
    port: int
    detail: str = ""

    def as_dict(self) -> dict:
        return {"running": self.running, "port": self.port, "detail": self.detail}


@dataclass
class CompanionServer:
    """Manages one gateway subprocess. Safe to construct more than once; the
    module-level :func:`get_companion_server` hands out a single instance so
    the backend lifespan and the API route agree on one child."""

    settings: Optional[Settings] = None
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False, compare=False)
    _gw_log: Optional[Path] = field(default=None, repr=False, compare=False)
    _last_error: str = ""

    def _s(self) -> Settings:
        return self.settings or get_settings()

    @property
    def port(self) -> int:
        return self._s().companion_port

    def start(self) -> GatewayStatus:
        """Start the gateway if LAN mode is on and it is not already running.

        Returns the current status; never raises. A failed start is reported
        in ``status.detail`` so the caller can surface it instead of pretending.
        """
        s = self._s()
        if not s.lan_mode:
            return GatewayStatus(False, self.port, "LAN mode is off")
        if self._alive():
            return self.status()

        try:
            cert_fp = ensure(s)
        except CompanionCertError as exc:
            self._last_error = f"certificate generation failed: {exc}"
            log.error("companion gateway not started: %s", self._last_error)
            return GatewayStatus(False, self.port, self._last_error)

        cert = Path(s.home) / "companion" / "companion.crt"
        key = Path(s.home) / "companion" / "companion.key"
        # stderr -> a log file, not a pipe: a pipe fills at 64KB and would block a
        # chatty child; a file we can tail on failure never does.
        gw_log = Path(s.home) / "companion" / "gateway.log"
        log_fh = open(gw_log, "ab", buffering=0)
        cmd = [
            sys.executable, "-m", "uvicorn",
            "jarvis.companion.gateway:build_companion_app", "--factory",
            "--host", "0.0.0.0",
            "--port", str(self.port),
            "--ssl-keyfile", str(key),
            "--ssl-certfile", str(cert),
            "--log-level", "info",
        ]
        # The gateway is package code, so any cwd works; use the jarvis package's
        # repo root so relative paths (if ever added) resolve predictably.
        repo_root = Path(__file__).resolve().parent.parent.parent
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(repo_root),
                stdout=subprocess.DEVNULL, stderr=log_fh,
                start_new_session=True,
            )
        except OSError as exc:
            self._proc = None
            log_fh.close()
            self._last_error = f"spawn failed: {exc}"
            log.error("companion gateway not started: %s", self._last_error)
            return GatewayStatus(False, self.port, self._last_error)
        self._gw_log = gw_log

        # Readiness: verify the TLS chain against the exact certificate we
        # generated (a MITM presenting a different cert still fails). The host
        # name is not required to match a self-signed LAN endpoint — see
        # :func:`_verified_context` for why that is the correct check here.
        try:
            client = httpx.Client(verify=_verified_context(cert), timeout=2.0)
        except ssl.SSLError as exc:
            self.stop()
            self._last_error = f"could not build TLS client context: {exc}"
            log.error("companion gateway not started: %s", self._last_error)
            return GatewayStatus(False, self.port, self._last_error)
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        url = f"https://127.0.0.1:{self.port}{HEALTH_PATH}"
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                tail = self._drain_stderr()
                self._last_error = f"process exited early (rc={self._proc.returncode}): {tail[-400:]}"
                self._proc = None
                log.error("companion gateway %s", self._last_error)
                return GatewayStatus(False, self.port, self._last_error)
            try:
                if client.get(url).status_code == 200:
                    return GatewayStatus(True, self.port, f"cert-fp {cert_fp[:16]}")
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        self.stop()
        self._last_error = f"no healthy response within {STARTUP_TIMEOUT_SECONDS:.0f}s"
        log.error("companion gateway: %s", self._last_error)
        return GatewayStatus(False, self.port, self._last_error)

    def stop(self) -> None:
        """Terminate the gateway (process group) and wait, escalating to SIGKILL."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("companion gateway did not exit after SIGKILL")

    def status(self) -> GatewayStatus:
        if self._alive():
            return GatewayStatus(True, self.port, "running")
        # Off: say *why*, so a "running: false" the panel shows is never a
        # mystery. LAN off is the common, intentional case.
        if not self._s().lan_mode:
            return GatewayStatus(False, self.port, "LAN mode is off")
        return GatewayStatus(False, self.port, self._last_error or "not started")

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _drain_stderr(self) -> str:
        # The child's stderr is a log file; read its tail on failure. A diagnostic
        # read must never raise or block.
        path = self._gw_log
        if not path or not path.exists():
            return ""
        try:
            data = path.read_bytes()
            return data[-1000:].decode("utf-8", "replace")
        except OSError:
            return ""


_server: Optional[CompanionServer] = None


def get_companion_server(settings: Optional[Settings] = None) -> CompanionServer:
    """The single gateway manager shared by the backend's lifespan and API.

    The instance is reused even when the caller passes a *different* ``Settings``
    object that means the same thing. The lifespan and the API routes each call
    this with their own ``get_settings()`` result, and every call to
    ``get_settings()``/``model_copy()`` produces a fresh object — so comparing by
    identity (``is not``) replaced the manager on every call, and the manager
    that had actually spawned the gateway was dropped without being stopped.
    That is how a gateway child survived its backend: the child kept
    ``0.0.0.0:8766`` bound with a certificate from a since-deleted test home,
    long after the process that started it was gone.

    A manager that already has a running child is therefore never replaced —
    only stopped explicitly (by ``stop()``), which is the only path that
    terminates the child.
    """
    global _server
    if _server is None:
        _server = CompanionServer(settings=settings)
    elif settings is not None and _server.settings is None:
        # The manager was created without settings (so it resolves them lazily);
        # pin them now so both callers agree on one configuration.
        _server.settings = settings
    return _server
