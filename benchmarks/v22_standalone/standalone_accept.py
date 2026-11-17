#!/usr/bin/env python3
"""V22 standalone acceptance: does the built bundle work *as a copy*?

The claim under test is that ``dist/Vantage.app`` is self-contained. A bundle that only
runs where it was built -- importing packages from the checkout's virtualenv, resolving
the native executor out of ``~/AppData/Vantage/native``, or serving from a working
directory inside the repository -- is not an application. Copying it somewhere else and
running the copy is the only way to tell the difference, because the original path is
still there to satisfy every one of those accidental dependencies.

So this harness:

1. copies the bundle to ``/tmp/vantage_standalone_test/Vantage.app``;
2. launches *the copy* with an isolated ``JARVIS_HOME`` on a port the dev port guard
   reports free, and polls ``/health`` until it answers (with a deadline);
3. reads the listening process's working directory and requires it to be inside the
   copy -- the assertion that matters;
4. asks ``GET /desktop`` and requires the executor path to be inside the copy;
5. reports the desktop capabilities honestly (a permission gap is recorded, not a
   failure and not a success);
6. stops what it started, confirms the port is released and the process is gone.

Exit status is 0 only when 1-4 and 6 pass. Step 5 never affects it.

Never port 8765. That port belongs to the user's installed app; a test build that takes
it displaces their running session. This script will not bind it even if asked to.

    python3 benchmarks/v22_standalone/standalone_accept.py [--keep] [--timeout S]

``--keep`` leaves the copied bundle and the isolated home on disk for inspection.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[2]
SOURCE_BUNDLE = REPO / "dist" / "Vantage.app"
PORT_GUARD = REPO / "scripts" / "dev_port_guard.sh"

SANDBOX = Path("/tmp/vantage_standalone_test")
COPIED_BUNDLE = SANDBOX / "Vantage.app"
TEST_HOME = Path("/tmp/vantage_standalone_home")

#: The installed app's backend. Refused unconditionally -- see the module docstring.
FORBIDDEN_PORT = 8765

#: Deadlines. Every wait in this file is bounded by one of these.
DEFAULT_BOOT_TIMEOUT = 180.0   # a cold bundle start compiles the mic probe helper
POLL_INTERVAL = 0.5
HEALTH_TIMEOUT = 3.0           # per request
SHUTDOWN_TIMEOUT = 30.0

TOKEN_HEADER = "X-JARVIS-Token"


class Step:
    """One row of the report."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.result = "BLOCKED"     # PASS / FAIL / BLOCKED
        self.evidence = ""

    def pass_(self, evidence: str) -> "Step":
        self.result, self.evidence = "PASS", evidence
        return self

    def fail(self, evidence: str) -> "Step":
        self.result, self.evidence = "FAIL", evidence
        return self

    def block(self, evidence: str) -> "Step":
        self.result, self.evidence = "BLOCKED", evidence
        return self


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def run(cmd: List[str], timeout: float = 20.0) -> Tuple[int, str]:
    """Run a helper command and return (exit code, combined output). Bounded."""
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def listener_pids(port: int) -> List[int]:
    """Pids listening on a TCP port. Empty when nothing does (or lsof cannot say)."""
    code, out = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=10.0)
    if code != 0:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def process_cwd(pid: int) -> Optional[str]:
    """The working directory of a process, or None when it cannot be read."""
    code, out = run(["lsof", "-nP", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=10.0)
    if code != 0 and not out:
        return None
    for line in out.splitlines():
        if line.startswith("n") and len(line) > 1:
            return line[1:]
    return None


def process_command(pid: int) -> str:
    code, out = run(["ps", "-p", str(pid), "-o", "command="], timeout=10.0)
    return out.strip() if code == 0 else ""


def pid_alive(pid: int) -> bool:
    """True when the pid names a process that has not exited.

    ``os.kill(pid, 0)`` is not enough: it succeeds for a zombie, and a child this
    process started stays a zombie until somebody calls ``wait()``. Reading the state
    from ``ps`` reports a defunct process as dead, which is what it is.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True         # alive, just not ours to signal
    code, out = run(["ps", "-p", str(pid), "-o", "stat="], timeout=10.0)
    if code != 0:
        return False        # gone between the two calls
    return not out.strip().startswith("Z")


def within(path: str, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it, after resolving symlinks.

    /tmp is a symlink to /private/tmp on macOS and the backend reports the resolved
    form, so both sides are resolved before comparing.
    """
    try:
        candidate = Path(path).resolve()
        anchor = root.resolve()
    except (OSError, RuntimeError):
        return False
    return candidate == anchor or anchor in candidate.parents


def http_json(url: str, token: str = "", timeout: float = HEALTH_TIMEOUT
              ) -> Tuple[Optional[int], Any]:
    """GET a JSON document. Returns (status, parsed body or error text)."""
    request = urllib.request.Request(url)
    if token:
        request.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except OSError:
            pass
        return exc.code, raw.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return status, f"unparseable body: {exc}"


def claim_port() -> Tuple[Optional[int], str]:
    """Ask the repository's dev port guard for a free port. Never 8765."""
    if not PORT_GUARD.is_file():
        return None, f"the port guard is missing at {PORT_GUARD}"
    code, out = run([str(PORT_GUARD), "claim"], timeout=20.0)
    if code != 0:
        return None, f"`{PORT_GUARD.name} claim` exited {code}: {out.strip()}"
    text = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not text.isdigit():
        return None, f"the port guard printed no port number: {out.strip()!r}"
    port = int(text)
    if port == FORBIDDEN_PORT:
        return None, f"the port guard offered the forbidden port {FORBIDDEN_PORT}"
    return port, f"scripts/dev_port_guard.sh claim -> {port}"


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------
class StandaloneTest:
    def __init__(self, keep: bool, boot_timeout: float) -> None:
        self.keep = keep
        self.boot_timeout = boot_timeout
        self.steps: Dict[str, Step] = {}
        self.port: Optional[int] = None
        self.spawned: List[int] = []      # pids this process started; never anything else
        self.process: Optional[subprocess.Popen] = None
        self.token = ""
        self.health: Dict[str, Any] = {}
        self.desktop: Dict[str, Any] = {}
        self.launch_log = SANDBOX / "launch.log"

    # -- bookkeeping --------------------------------------------------------
    def step(self, key: str, name: str) -> Step:
        step = Step(name)
        self.steps[key] = step
        return step

    def reap(self) -> None:
        """Collect the launcher's exit status so it stops being a zombie.

        Only a child of this process can be waited on, and only the launcher is our
        child. The backend it starts belongs to the launcher, not to us.
        """
        process = self.process
        if process is None:
            return
        try:
            process.poll()
        except OSError:
            pass

    # -- 1. copy ------------------------------------------------------------
    def copy_bundle(self) -> bool:
        step = self.step("copy", "1. copy the bundle to an isolated location")
        if not SOURCE_BUNDLE.is_dir():
            return bool(step.block(f"no bundle at {SOURCE_BUNDLE} -- nothing to test"))
        if not (SOURCE_BUNDLE / "Contents" / "MacOS").is_dir():
            return bool(step.block(f"{SOURCE_BUNDLE} is not an .app bundle"))

        try:
            if SANDBOX.exists():
                shutil.rmtree(SANDBOX)
            SANDBOX.mkdir(parents=True)
            started = time.time()
            # ditto preserves the resource forks, xattrs and symlinks a signed bundle
            # needs; a codesign --verify after the copy is the check that it did.
            code, out = run(["/usr/bin/ditto", str(SOURCE_BUNDLE), str(COPIED_BUNDLE)],
                            timeout=300.0)
            if code != 0:
                return bool(step.fail(f"ditto exited {code}: {out.strip()[:300]}"))
            elapsed = time.time() - started
        except OSError as exc:
            return bool(step.fail(f"copy failed: {type(exc).__name__}: {exc}"))

        # The copy must still satisfy its own signature, or it is not the same bundle.
        code, out = run(["/usr/bin/codesign", "--verify", str(COPIED_BUNDLE)], timeout=60.0)
        signature = "signature verifies" if code == 0 else f"codesign --verify exited {code}"

        code, ident = run(["/usr/bin/codesign", "-dv", str(COPIED_BUNDLE)], timeout=60.0)
        bundle_id = ""
        for line in ident.splitlines():
            if line.startswith("Identifier="):
                bundle_id = line.split("=", 1)[1].strip()

        copied_files = sum(1 for _ in COPIED_BUNDLE.rglob("*"))
        evidence = (f"{SOURCE_BUNDLE} -> {COPIED_BUNDLE} in {elapsed:.1f}s, "
                    f"{copied_files} entries, {signature}")
        if bundle_id:
            evidence += f", identity {bundle_id}"
        # A copy that does not verify is still worth running (an ad-hoc signature can be
        # invalidated by things that do not affect execution), but it is not a pass.
        if code == 0:
            return bool(step.pass_(evidence))
        return bool(step.fail(evidence))

    # -- launch -------------------------------------------------------------
    def launch(self) -> bool:
        """Start the copy and wait for /health. Returns True when it answered."""
        step = self.step("launch", "2. launch the copy and reach /health")
        if self.steps["copy"].result != "PASS":
            return bool(step.block("the copy did not succeed"))

        port, claim_note = claim_port()
        if port is None:
            return bool(step.block(claim_note))
        self.port = port

        # The guard reports a free port; it does not reserve it. Re-check under lsof so a
        # race is a clear message rather than a backend that fails to bind.
        holders = listener_pids(port)
        if holders:
            return bool(step.block(f"port {port} was claimed but {holders} is listening"))

        try:
            if TEST_HOME.exists():
                shutil.rmtree(TEST_HOME)
            TEST_HOME.mkdir(parents=True)
        except OSError as exc:
            return bool(step.fail(f"could not create the isolated home: {exc}"))

        env = dict(os.environ)
        # Do not let the caller's shell decide where the test backend keeps state or what
        # it binds. In particular JARVIS_PORT/HOME from this shell would defeat both.
        for name in ("JARVIS_HOME", "JARVIS_PORT", "JARVIS_HOST", "JARVIS_API_TOKEN",
                     "JARVIS_PROJECT_DIR", "JARVIS_PYTHON", "VANTAGE_DESKTOP_EXECUTOR"):
            env.pop(name, None)
        env["JARVIS_HOME"] = str(TEST_HOME)
        env["JARVIS_PORT"] = str(port)
        env["JARVIS_HOST"] = "127.0.0.1"
        # Headless: the copy runs its server without a Tk window. The control panel is a
        # convenience; the backend is what this test is about, and a Tk window cannot be
        # driven from here.
        env["JARVIS_HEADLESS"] = "1"
        # The headless path opens the dashboard in a browser. A test that pops a browser
        # window is a test that interrupts whoever is working.
        env["BROWSER"] = "/usr/bin/true"

        self.launch_log.parent.mkdir(parents=True, exist_ok=True)
        handle = self.launch_log.open("wb")
        started = time.time()
        try:
            process = subprocess.Popen(
                [str(COPIED_BUNDLE / "Contents" / "MacOS" / "JarvisForge")],
                stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, cwd=str(SANDBOX), start_new_session=True,
            )
        except OSError as exc:
            handle.close()
            return bool(step.fail(f"could not execute the launcher: {exc}"))
        finally:
            try:
                handle.close()
            except OSError:
                pass

        self.spawned.append(process.pid)
        self.process = process

        base = f"http://127.0.0.1:{port}"
        deadline = started + self.boot_timeout
        last_error = "no attempt made"
        while time.time() < deadline:
            if process.poll() is not None:
                tail = self.log_tail(20)
                return bool(step.fail(
                    f"the launcher exited (code {process.returncode}) before /health "
                    f"answered, after {time.time() - started:.1f}s. log tail:\n{tail}"))
            status, body = http_json(f"{base}/health/identity", timeout=HEALTH_TIMEOUT)
            if status == 200 and isinstance(body, dict):
                break
            last_error = str(body)[:200]
            time.sleep(POLL_INTERVAL)
        else:
            tail = self.log_tail(20)
            return bool(step.fail(
                f"/health did not answer within {self.boot_timeout:g}s "
                f"(last error: {last_error}). log tail:\n{tail}"))

        elapsed = time.time() - started

        # /health itself is authenticated. The token is generated by the backend into the
        # isolated home on first use, so read it from there rather than inventing one.
        token_file = TEST_HOME / "api_token"
        token_deadline = time.time() + 10.0
        while time.time() < token_deadline and not token_file.exists():
            time.sleep(POLL_INTERVAL)
        try:
            self.token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return bool(step.fail(f"the backend answered but {token_file} is unreadable: {exc}"))

        health_deadline = time.time() + 15.0
        status, body = None, ""
        while time.time() < health_deadline:
            status, body = http_json(f"{base}/health", token=self.token)
            if status == 200 and isinstance(body, dict):
                break
            time.sleep(POLL_INTERVAL)
        if status != 200 or not isinstance(body, dict):
            return bool(step.fail(
                f"/health/identity answered, but authenticated /health returned "
                f"{status}: {str(body)[:200]}"))

        self.health = body
        self.health["_elapsed"] = round(elapsed, 1)
        return bool(step.pass_(
            f"answered /health in {elapsed:.1f}s on port {port} "
            f"({claim_note}); status={body.get('status')!r}, "
            f"{body.get('tools')} tools"))

    def log_tail(self, lines: int) -> str:
        try:
            text = self.launch_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no launcher log)"
        tail = [line for line in text.splitlines() if line.strip()][-lines:]
        return "\n".join("      " + line for line in tail) or "(launcher log is empty)"

    # -- 3. working directory ----------------------------------------------
    def check_cwd(self) -> bool:
        step = self.step("cwd", "3. the backend serves from inside the copy")
        if self.steps["launch"].result != "PASS" or self.port is None:
            return bool(step.block("the backend never came up"))

        pids = listener_pids(self.port)
        if not pids:
            return bool(step.fail(f"nothing is listening on {self.port} any more"))

        observed = []
        offending = []
        for pid in pids:
            cwd = process_cwd(pid)
            observed.append((pid, cwd or "(unreadable)"))
            if not cwd or not within(cwd, COPIED_BUNDLE):
                offending.append((pid, cwd))

        rendered = "; ".join(f"pid {pid} cwd {cwd}" for pid, cwd in observed)
        if offending:
            return bool(step.fail(
                f"listening process is NOT inside the copy: {rendered} "
                f"(expected a path under {COPIED_BUNDLE.resolve()})"))

        # The identity route names the source root the process imported from. It is a
        # second, independent reading of the same claim, and it catches a process whose
        # cwd is right but whose code came from the checkout.
        root = ""
        status, body = http_json(f"http://127.0.0.1:{self.port}/health/identity")
        if status == 200 and isinstance(body, dict):
            root = str((body.get("identity") or {}).get("source_root") or "")
        copied_app = COPIED_BUNDLE / "Contents" / "Resources" / "app"
        if not root:
            return bool(step.fail(
                f"cwd is inside the copy ({rendered}), but /health/identity reported no "
                f"source_root, so the loaded code cannot be placed"))
        if not within(root, copied_app):
            return bool(step.fail(
                f"cwd is inside the copy ({rendered}), but the running code came from "
                f"{root} -- not the copy's {copied_app}"))
        return bool(step.pass_(f"{rendered}; source_root {root}"))

    # -- 4. executor --------------------------------------------------------
    def check_executor(self) -> bool:
        step = self.step("executor", "4. the native executor resolves inside the copy")
        if self.steps["launch"].result != "PASS" or self.port is None:
            return bool(step.block("the backend never came up"))

        status, body = http_json(f"http://127.0.0.1:{self.port}/desktop", token=self.token)
        if status != 200 or not isinstance(body, dict):
            return bool(step.fail(f"GET /desktop returned {status}: {str(body)[:300]}"))
        self.desktop = body

        executable = str(body.get("executor") or "")
        if not executable:
            return bool(step.fail(
                "GET /desktop reported no executor path -- the capability is missing, "
                f"and the payload says: {str(body.get('reason') or body)[:200]}"))
        if not within(executable, COPIED_BUNDLE):
            return bool(step.fail(
                f"executor {executable} is outside the copy ({COPIED_BUNDLE.resolve()})"))
        expected = (COPIED_BUNDLE / "Contents" / "Resources" / "app" / "native"
                    / "desktop_executor" / "vantage-desktop")
        if not Path(executable).is_file():
            return bool(step.fail(f"executor {executable} is inside the copy but does not exist"))
        return bool(step.pass_(
            f"GET /desktop -> executor {executable}"
            + ("" if Path(executable).resolve() == expected.resolve()
               else f" (expected {expected})")))

    # -- 5. permissions (reported, never scored) ----------------------------
    def report_permissions(self) -> None:
        step = self.step("permissions", "5. desktop capabilities (reported, not scored)")
        if self.steps["launch"].result != "PASS" or self.port is None:
            return bool(step.block("the backend never came up, so there is nothing to report"))
        if not self.desktop:
            return bool(step.block("GET /desktop did not answer"))

        capabilities = self.desktop.get("capabilities") or {}
        flags = ", ".join(f"{name}: {bool(capabilities.get(name))}"
                          for name in ("move_cursor", "click", "type", "read_ui",
                                       "screen_capture"))
        # The posture route does not carry the executor's identity; /desktop/identity
        # does. It is a separate read because it describes a different thing: not what
        # this machine allows, but which binary macOS was asked about.
        identity_bits = []
        status, identity = http_json(f"http://127.0.0.1:{self.port}/desktop/identity",
                                     token=self.token)
        if status == 200 and isinstance(identity, dict):
            for key in ("bundle_id", "signing", "executable"):
                if identity.get(key):
                    identity_bits.append(f"{key} {identity[key]}")
        identity_text = "; ".join(identity_bits) or "identity unavailable"
        self.desktop["_identity"] = identity

        note = ""
        if capabilities.get("click"):
            # Say *why* a copy reports a grant it was never individually given. The bundle
            # is ad-hoc signed (`codesign --sign -`), so its CDHash is a function of its
            # contents: a byte-identical copy carries a byte-identical identity, and TCC
            # answers about the identity, not the path. This is a real capability of the
            # copy, but it is inherited -- it proves nothing about granting a fresh copy.
            original = self.codesign_identity(SOURCE_BUNDLE)
            copied = self.codesign_identity(COPIED_BUNDLE)
            if original and original == copied:
                note = (f" -- this copy reports a grant because its ad-hoc CDHash "
                        f"({copied}) is identical to the original's, so macOS answers for "
                        f"the same code identity. That is inherited, not newly granted, and "
                        f"it does not show that an unapproved copy would work")
            else:
                note = (" -- click/type true here; a copy whose CDHash differs from a "
                        "granted build is the case where macOS would refuse it")
        else:
            # Deliberate: a Finder-launched copy whose identity macOS has not granted is
            # refused post-event access. Packaging is fine; the grant is what is missing,
            # and this test cannot make it.
            note = (" -- click/type false is the expected result for a copy macOS has not "
                    "granted post-event access to; that is a permission gap, not a "
                    "packaging failure, and this run neither fixes nor proves it")
        return bool(step.pass_(f"{flags} ({identity_text}){note}"))

    @staticmethod
    def codesign_identity(bundle: Path) -> str:
        """The bundle's CDHash, or "" when it cannot be read. Used only for comparison."""
        code, out = run(["/usr/bin/codesign", "-dvvv", str(bundle)], timeout=30.0)
        if code != 0 and not out:
            return ""
        for line in out.splitlines():
            if line.startswith("CDHash="):
                return line.split("=", 1)[1].strip()
        return ""

    # -- 6. shutdown --------------------------------------------------------
    def shutdown(self) -> bool:
        step = self.step("shutdown", "6. shut down cleanly")
        if not self.spawned and self.port is None:
            return bool(step.block("nothing was started, so there is nothing to stop"))

        # Only pids this process started are signalled. A listener on the test port that
        # is not one of ours is left alone and reported instead.
        targets = [pid for pid in self.spawned if pid_alive(pid)]
        for pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                return bool(step.fail(f"pid {pid} is alive but not ours to signal"))

        deadline = time.time() + SHUTDOWN_TIMEOUT
        while time.time() < deadline and any(pid_alive(pid) for pid in targets):
            self.reap()             # promote the zombie to a real exit
            time.sleep(POLL_INTERVAL)

        survivors = [pid for pid in targets if pid_alive(pid)]
        if survivors:
            for pid in survivors:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            grace = time.time() + 5.0
            while time.time() < grace and any(pid_alive(pid) for pid in survivors):
                self.reap()
                time.sleep(POLL_INTERVAL)
            survivors = [pid for pid in survivors if pid_alive(pid)]
            if survivors:
                return bool(step.fail(
                    f"pids {survivors} survived SIGTERM and SIGKILL"))
        self.reap()

        # The port has to be released, not merely unused by us. A backend that leaked a
        # child would still hold it.
        port_note = "no port was claimed"
        port_released = True
        if self.port is not None:
            release_deadline = time.time() + 15.0
            holders = listener_pids(self.port)
            while time.time() < release_deadline and holders:
                time.sleep(POLL_INTERVAL)
                holders = listener_pids(self.port)
            port_released = not holders
            port_note = (f"port {self.port} released" if port_released
                         else f"port {self.port} still held by {holders}")

        app_exited = all(not pid_alive(pid) for pid in self.spawned)

        # An exit code is the launcher's own verdict on its shutdown. A clean stop returns
        # 0. A non-zero code from a SIGTERM means the signal handler raised, which is a
        # failing stop path even when the process did die.
        exit_code = self.process.returncode if self.process is not None else None
        crash = self.crash_detail()

        # Who stopped the backend? The launcher's own stop() terminates it; the guardian
        # thread only notices afterwards and exits the server because its parent went
        # away. Both release the port, so a released port alone does not prove the
        # shutdown was orderly -- and this is exactly the case this run hit.
        # Three distinct stop paths, and only one of them is a failure:
        #   supervisor  -- runtime.stop() stopped it; the subprocess is already gone
        #                  by the time stop() checks, so "stopping backend server"
        #                  is never logged. This is the orderly path.
        #   terminate   -- the launcher terminated the subprocess itself.
        #   guardian    -- neither ran; run.py noticed the dead parent. A safety net,
        #                  not a shutdown.
        text = self.log_text()
        # The supervisor logs its own words, not the launcher's. Matching only the
        # launcher's string reported "neither stop path ran" while the log plainly
        # showed "runtime supervisor stopping" -> "runtime supervisor stopped".
        tried_supervisor = any(marker in text for marker in (
            "requesting runtime shutdown via supervisor",
            "runtime supervisor stopping",
            "runtime supervisor stopped"))
        tried_terminate = "stopping backend server" in text

        evidence = (f"SIGTERM to {targets or 'no pid'}; app exited: {app_exited} "
                    f"(exit code {exit_code}); backend stopped by "
                    f"{'the supervisor (orderly)' if tried_supervisor else ('the launcher' if tried_terminate else 'the guardian (neither stop path ran)')}; "
                    f"{port_note}")
        if crash:
            evidence += f"\nlauncher stop path raised: {crash}"
        if crash or exit_code not in (0, -signal.SIGTERM):
            return bool(step.fail(evidence))
        if app_exited and port_released:
            return bool(step.pass_(evidence))
        return bool(step.fail(evidence))

    def log_text(self) -> str:
        try:
            return self.launch_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def crash_detail(self) -> str:
        """The launcher's own unhandled exception during shutdown, if it raised one.

        Read from the captured log rather than inferred: a shutdown that released the
        port but crashed on the way out is not a clean shutdown, and the next reader
        deserves the exception that says why.
        """
        text = self.log_text()
        if "Traceback (most recent call last)" not in text:
            return ""
        # The exception that ended the last traceback is the last non-blank line that is
        # not a source frame or a caret underline. Reporting anything else would name a
        # frame's file or a "During handling..." header as if it were the failure.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith(("Traceback", "File ", "^", "~", "During", "during")):
                continue
            if "Error" in line or line.endswith(("Exception", "Warning")):
                return line[:200]
        return "an unhandled exception (see the launcher log)"

    # -- 7. cleanup ---------------------------------------------------------
    def cleanup(self) -> str:
        """Remove what this run created, unless --keep. Never touches anything else."""
        removed = []
        if self.keep:
            return f"kept: {COPIED_BUNDLE} and {TEST_HOME}"
        for path in (SANDBOX, TEST_HOME):
            try:
                if path.exists():
                    shutil.rmtree(path)
                    removed.append(str(path))
            except OSError as exc:
                return f"could not remove {path}: {exc}"
        return f"removed {', '.join(removed)}" if removed else "nothing to remove"

    # -- report -------------------------------------------------------------
    def report(self, cleanup_note: str) -> int:
        order = ["copy", "launch", "cwd", "executor", "permissions", "shutdown"]
        print()
        print("V22 standalone acceptance")
        print(f"  bundle : {SOURCE_BUNDLE}")
        print(f"  copy   : {COPIED_BUNDLE}")
        print(f"  home   : {TEST_HOME}")
        print(f"  port   : {self.port if self.port is not None else '(none claimed)'}")
        print(f"  log    : {self.launch_log if SANDBOX.exists() else '(cleaned up)'}")
        print()
        width = max(len(self.steps[key].name) for key in order if key in self.steps)
        print(f"  {'step'.ljust(width)}  result   evidence")
        print(f"  {'-' * width}  ------   --------")
        for key in order:
            step = self.steps.get(key)
            if step is None:
                continue
            lines = step.evidence.splitlines() or [""]
            print(f"  {step.name.ljust(width)}  {step.result:<7}  {lines[0]}")
            for line in lines[1:]:
                print(f"  {' ' * width}           {line}")
        print()
        print(f"  cleanup: {cleanup_note}")

        scored = ["copy", "launch", "cwd", "executor", "shutdown"]
        failures = [key for key in scored
                    if self.steps.get(key) is None or self.steps[key].result != "PASS"]
        print()
        if failures:
            print(f"  RESULT: FAIL ({', '.join(failures)} did not pass)")
            return 1
        permissions = self.steps.get("permissions")
        print("  RESULT: PASS -- the copy is independent of this checkout.")
        if permissions is not None and permissions.result == "PASS":
            print("          Step 5 is informational; permission state does not affect "
                  "this exit status.")
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the built Vantage.app runs as a standalone copy.")
    parser.add_argument("--keep", action="store_true",
                        help="leave the copied bundle and isolated home on disk")
    parser.add_argument("--timeout", type=float, default=DEFAULT_BOOT_TIMEOUT,
                        help=f"seconds to wait for /health (default {DEFAULT_BOOT_TIMEOUT:.0f})")
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2

    test = StandaloneTest(keep=args.keep, boot_timeout=args.timeout)
    exit_code = 1
    try:
        test.copy_bundle()
        if test.launch():
            test.check_cwd()
            test.check_executor()
        test.report_permissions()
    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
    except Exception as exc:  # a harness bug must still clean up and report
        print(f"  harness error: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        try:
            test.shutdown()
        except Exception as exc:
            print(f"  shutdown error: {type(exc).__name__}: {exc}", file=sys.stderr)
        cleanup_note = test.cleanup()
        exit_code = test.report(cleanup_note)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
