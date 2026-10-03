#!/usr/bin/env python3
"""Vantage application launcher.

This is what the ``Vantage.app`` bundle executes. It:

1. resolves the project directory and a Python interpreter,
2. starts the Vantage server as a managed subprocess,
3. shows a small control window with live status,
4. opens the dashboard in the browser,
5. shuts the server down cleanly when you quit.

Running from inside a signed app bundle is also what lets macOS attach
Speech Recognition and Microphone permissions to Vantage -- a bare command-line
process is refused by TCC.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BUNDLE_ROOT = Path(__file__).resolve().parents[1]  # .../Vantage.app/Contents


def _resource(name: str) -> Optional[Path]:
    candidate = BUNDLE_ROOT / "Resources" / name
    return candidate if candidate.exists() else None


def resolve_project_dir() -> Path:
    """Find the jarvis-forge source tree."""
    # 1. Explicit override (useful for development).
    env = os.environ.get("JARVIS_PROJECT_DIR")
    if env and Path(env).expanduser().is_dir():
        return Path(env).expanduser().resolve()

    # 2. The copy staged inside this bundle. Checked before the recorded path because it
    #    is the one that travels: a bundle that carries its own source is an application,
    #    while a pointer at someone's checkout is a shortcut that breaks when the
    #    checkout moves or the app is copied to another user's machine.
    staged = BUNDLE_ROOT / "Resources" / "app"
    if (staged / "run.py").exists() and (staged / "jarvis").is_dir():
        return staged.resolve()

    # 3. Path recorded at build time inside the bundle.
    recorded = _resource("project_path.txt")
    if recorded:
        text = recorded.read_text(encoding="utf-8").strip()
        if text and Path(text).is_dir():
            return Path(text)

    # 4. Look at the bundle's own location and the three directories above it.
    #    BUNDLE_ROOT is "<checkout>/Contents" when the launcher runs from source and
    #    "<checkout>/dist/Vantage.app/Contents" in a build -- so the checkout is
    #    the third ancestor. The old list (root, parent, grandparent) stopped at
    #    dist/, which meant a build inside its own tree depended entirely on the
    #    pointer recorded at build time, and relocating the checkout broke an app
    #    that had never moved.
    for candidate in (BUNDLE_ROOT, *BUNDLE_ROOT.parents[:3]):
        if (candidate / "run.py").exists() and (candidate / "jarvis").is_dir():
            return candidate.resolve()

    raise RuntimeError(
        "Could not locate the jarvis-forge project directory. "
        "Set JARVIS_PROJECT_DIR or rebuild the app with build_app.py."
    )


def resolve_python(project_dir: Path) -> str:
    """Find an interpreter that can run the server."""
    recorded = _resource("python_path.txt")
    recorded_path = recorded.read_text(encoding="utf-8").strip() if recorded else None

    candidates = [
        os.environ.get("JARVIS_PYTHON"),
        recorded_path,
        str(project_dir / ".venv" / "bin" / "python"),
        str(Path.home() / ".workbuddy-ai/binaries/python/envs/default/bin/python"),
        sys.executable,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return "python3"


# ---------------------------------------------------------------------------
# Tk / Tcl bootstrap
# ---------------------------------------------------------------------------
class GuiUnavailable(RuntimeError):
    """Raised when a control window cannot be created on this machine."""


def prepare_tk_environment() -> None:
    """Point Tcl/Tk at the interpreter's bundled libraries.

    Python normally finds these itself, but that resolution depends on the
    interpreter's prefix, which is not always what you expect when the process
    was launched from inside an application bundle. Pinning the variables
    explicitly costs nothing and removes a whole class of "Cannot find a usable
    init.tcl" failures.
    """
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return

    for lib_root in (
        Path(sys.base_prefix) / "lib",
        Path(sys.prefix) / "lib",
    ):
        if not lib_root.is_dir():
            continue
        for tcl_dir in sorted(lib_root.glob("tcl[0-9]*"), reverse=True):
            if (tcl_dir / "init.tcl").exists():
                os.environ.setdefault("TCL_LIBRARY", str(tcl_dir))
                break
        for tk_dir in sorted(lib_root.glob("tk[0-9]*"), reverse=True):
            if (tk_dir / "tk.tcl").exists():
                os.environ.setdefault("TK_LIBRARY", str(tk_dir))
                break
        if os.environ.get("TCL_LIBRARY"):
            return


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------
def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def data_home() -> Path:
    """Where this install keeps its files -- the same directory as the token."""
    configured = os.environ.get("JARVIS_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".jarvis-forge"


def lan_switch_state() -> bool:
    """Whether the user has already allowed devices on their wifi.

    Written as a plain word rather than imported from `jarvis.config`: the control panel
    has to work when the server it manages does not, and a test pins the two readers to
    the same file so they cannot drift.
    """
    try:
        return (data_home() / "lan_mode").read_text(encoding="utf-8").strip().lower() == "true"
    except OSError:
        return False


def set_lan_switch(enabled: bool) -> Path:
    path = data_home() / "lan_mode"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("true\n" if enabled else "false\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def pairing_qr_png(url: str) -> Optional[bytes]:
    """Draw a scan code for the pairing URL, or None when the encoder is absent.

    `qrcode` is deliberately not a dependency of a stdlib-first app: on a clean install the
    panel shows the address as text, which is a smaller code, not a crash. The missing
    library path is tested because an untested fallback is a bug waiting for someone else.
    """
    cached = _QR_CACHE.get(url, "unset")
    if cached != "unset":
        return cached  # type: ignore[return-value]
    try:
        import io

        import qrcode
    except ImportError:
        _QR_CACHE[url] = None
        return None
    buffer = io.BytesIO()
    qrcode.make(url).save(buffer, format="PNG")
    _QR_CACHE[url] = buffer.getvalue()
    return _QR_CACHE[url]


def lan_phone_url(urls: list) -> str:
    """The one address worth showing a phone: reachable from another device, not loopback."""
    for url in urls or []:
        if "127.0.0.1" in url or "localhost" in url or "::1" in url:
            continue
        return url
    return ""


_QR_CACHE: dict = {}


def _api_token() -> str:
    """Read the per-install API token so the launcher can talk to the server.

    Checks the configured data directory first, then the default one -- when we
    attach to an already-running server it may have been started with a
    different JARVIS_HOME, and its token lives there.
    """
    explicit = os.environ.get("JARVIS_API_TOKEN")
    if explicit:
        return explicit.strip()

    candidates = []
    configured = os.environ.get("JARVIS_HOME")
    if configured:
        candidates.append(Path(configured))
    default_home = Path.home() / ".jarvis-forge"
    if default_home not in candidates:
        candidates.append(default_home)

    for home in candidates:
        token_file = home / "api_token"
        try:
            if token_file.exists():
                value = token_file.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return ""


def _auth_headers(extra: Optional[dict] = None) -> dict:
    headers = dict(extra or {})
    token = _api_token()
    if token:
        headers["X-JARVIS-Token"] = token
    return headers


def api_get(host: str, port: int, path: str, timeout: float = 3.0):
    url = f"http://{host}:{port}{path}"
    request = urllib.request.Request(url, headers=_auth_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def api_post(host: str, port: int, path: str, payload: dict, timeout: float = 60.0):
    url = f"http://{host}:{port}{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=_auth_headers({"Content-Type": "application/json"}),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


MIC_WORDS = {
    "ready": "ready",
    "measuring": "measuring now",
    "blocked": "blocked by macOS permission",
    "no_backend": "no capture backend installed",
    "unknown": "permission state unreadable",
}


def mic_note(audio: dict) -> str:
    """One honest line about the microphone, from a /voice/status payload.

    "A capture binary exists" and "Vantage is allowed to listen" are separate
    facts, so this never reports the first as if it were the second.
    """
    mic = audio.get("microphone") or {}
    state = audio.get("capture_state")
    if not state:
        # An older server reports only the boolean; guessing a cause here would
        # blame permission for what might be a missing backend.
        state = "ready" if audio.get("capture_available") else "unknown"
    word = MIC_WORDS.get(state, state)
    devices = mic.get("devices") or []
    if state == "ready":
        return f"{word} ({len(devices)} input device(s))" if devices else word
    detail = mic.get("detail") or mic.get("reason") or ""
    return f"{word} — {detail}" if detail else word


def settled_voice_status(host: str, port: int, timeout: float = 25.0) -> dict:
    """Read /voice/status, waiting for the microphone probe to have an answer.

    Consent is measured on a background thread because the first reading can
    mean compiling Swift and waiting out macOS's assessment of a new binary.
    A health check that sampled the interim "still measuring" would report a
    healthy machine as broken, purely because it asked too early.
    """
    deadline = time.time() + timeout
    voice: dict = {}
    while True:
        voice = api_get(host, port, "/voice/status")
        state = (voice.get("audio") or {}).get("capture_state")
        if state not in (None, "unknown", "measuring") or time.time() >= deadline:
            return voice
        time.sleep(0.5)


class ServerController:
    """Starts, monitors and stops the Vantage server process."""

    def __init__(self, project_dir: Path, python: str, host: str, port: int, log_path: Path) -> None:
        self.project_dir = project_dir
        self.python = python
        self.host = host
        self.port = port
        #: What the server binds. Distinct from `host`, which is the address the panel and
        #: the browser use to *reach* it -- `http://0.0.0.0:8765` is not a page anyone can
        #: open, and a laptop that only binds loopback cannot be reached by a phone at all.
        #: So the wifi switch moves the bind, never the link shown to the user.
        self.bind_host = self._initial_bind(host)
        self.log_path = log_path
        self.process: Optional[subprocess.Popen] = None
        self.attached = False  # True when we adopted an already-running server
        #: Why the project directory could not be found, when that happened. The
        #: self-test needs it: a report that only says "no such directory" leaves the
        #: user with a path they already know is wrong.
        self.project_dir_error = ""
        self._log_handle = None
        self.tail: list[str] = []

    # -- lifecycle ----------------------------------------------------------
    @staticmethod
    def _initial_bind(host: str) -> str:
        """Honour a saved wifi switch on every launch, and an explicit env var first."""
        explicit = os.environ.get("JARVIS_HOST")
        if explicit:
            return explicit
        return "0.0.0.0" if lan_switch_state() else host

    def start(self) -> None:
        if port_in_use(self.host, self.port):
            self.attached = True
            self._note(f"Attached to an existing server on {self.host}:{self.port}")
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
        self._log_handle.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # A bundle is signed once, after its program has been staged. If the server it
        # spawns compiles that program, the .pyc files land inside the sealed folder and
        # the copy the user then makes fails `codesign --verify`.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # The server watches this pid. If the panel disappears on a path Python never
        # sees -- Tk's own `exit`, a crash, a SIGKILL -- the child would otherwise be
        # adopted by launchd and keep listening on the wifi address.
        env["JARVIS_GUARDIAN_PID"] = str(os.getpid())
        env.setdefault("JARVIS_HOST", self.bind_host)
        env.setdefault("JARVIS_PORT", str(self.port))

        self.process = subprocess.Popen(
            [self.python, "run.py"],
            cwd=str(self.project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
        )
        self._note(f"Started server (pid {self.process.pid})")

        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Forward server output into the log file and the UI tail."""
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.rstrip("\n")
            if self._log_handle:
                self._log_handle.write(line + "\n")
            self.tail.append(line)
            del self.tail[:-400]

    def wait_ready(self, timeout: float = 45.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                api_get(self.host, self.port, "/health", timeout=2.0)
                return True
            except Exception:
                if self.process and self.process.poll() is not None and not self.attached:
                    return False
                time.sleep(0.4)
        return False

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self._note("Server stopped")
        if self._log_handle:
            try:
                self._log_handle.close()
            except OSError:
                pass

    def restart(self) -> None:
        self.stop()
        self.process = None
        self.attached = False
        self.tail.clear()
        self.start()

    def running(self) -> bool:
        if self.attached:
            return port_in_use(self.host, self.port)
        return bool(self.process and self.process.poll() is None)

    def _note(self, message: str) -> None:
        self.tail.append(f"[launcher] {message}")
        del self.tail[:-400]


# ---------------------------------------------------------------------------
# Control panel
# ---------------------------------------------------------------------------
def build_ui(controller: ServerController, on_quit) -> None:
    prepare_tk_environment()

    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext
    except ImportError as exc:
        raise GuiUnavailable(f"tkinter is not available in this Python: {exc}") from exc

    BG = "#f5f7fa"
    PANEL = "#ffffff"
    INK = "#1b2230"
    MUTED = "#6b7688"
    ACCENT = "#2f6df6"
    OK = "#16a34a"
    BAD = "#dc2626"

    try:
        root = tk.Tk()
    except Exception as exc:
        raise GuiUnavailable(str(exc)) from exc

    root.title("Vantage")
    root.configure(bg=BG)
    root.geometry("560x460")
    root.minsize(480, 400)

    try:
        icon = _resource("JarvisForge.icns")
        if icon:
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
    except Exception:
        pass  # icon is cosmetic

    # --- header ------------------------------------------------------------
    header = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground="#e2e7ef")
    header.pack(fill="x")

    tk.Label(
        header, text="Vantage", bg=PANEL, fg=INK,
        font=("Helvetica Neue", 18, "bold"),
    ).pack(anchor="w", padx=16, pady=(12, 0))

    status_row = tk.Frame(header, bg=PANEL)
    status_row.pack(anchor="w", padx=16, pady=(2, 12))

    dot = tk.Label(status_row, text="\u25cf", bg=PANEL, fg=MUTED, font=("Helvetica", 13))
    dot.pack(side="left")
    status = tk.Label(
        status_row, text="starting…", bg=PANEL, fg=MUTED, font=("Helvetica Neue", 12)
    )
    status.pack(side="left", padx=(6, 0))

    # --- stats -------------------------------------------------------------
    stats = tk.Frame(root, bg=BG)
    stats.pack(fill="x", padx=14, pady=(12, 0))

    stat_labels = {}
    for index, (key, title) in enumerate(
        [("devices", "Devices"), ("tools", "Tools"), ("pending", "Pending"), ("llm", "Model")]
    ):
        card = tk.Frame(stats, bg=PANEL, highlightthickness=1, highlightbackground="#e2e7ef")
        card.grid(row=0, column=index, sticky="nsew", padx=4)
        stats.columnconfigure(index, weight=1)
        tk.Label(card, text=title.upper(), bg=PANEL, fg=MUTED,
                 font=("Helvetica Neue", 9, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
        value = tk.Label(card, text="—", bg=PANEL, fg=INK,
                         font=("Helvetica Neue", 16, "bold"))
        value.pack(anchor="w", padx=10, pady=(0, 8))
        stat_labels[key] = value

    # --- buttons -----------------------------------------------------------
    buttons = tk.Frame(root, bg=BG)
    buttons.pack(fill="x", padx=14, pady=12)

    def make_button(parent, text, command, primary=False):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=ACCENT if primary else PANEL,
            fg="#ffffff" if primary else INK,
            activebackground=ACCENT if primary else "#eef1f6",
            activeforeground="#ffffff" if primary else INK,
            relief="flat",
            highlightthickness=0 if primary else 1,
            highlightbackground="#e2e7ef",
            font=("Helvetica Neue", 12),
            padx=14,
            pady=7,
            cursor="hand2",
        )

    def open_dashboard():
        webbrowser.open(f"http://{controller.host}:{controller.port}/dashboard")

    def voice_status():
        try:
            info = api_get(controller.host, controller.port, "/voice/status")
            tts = [k for k, v in info["tts"]["available"].items() if v]
            stt = [k for k, v in info["stt"]["available"].items() if v]
            messagebox.showinfo(
                "Voice status",
                f"Text to speech: {', '.join(tts) or 'none'}\n"
                f"Speech to text: {', '.join(stt) or 'none'}\n"
                f"Microphone: {mic_note(info['audio'])}\n"
                f"Wake words: {', '.join(info['wake']['wake_words'])}",
            )
        except Exception as exc:
            messagebox.showerror("Voice status", f"Could not read voice status:\n{exc}")

    def speak_test():
        try:
            api_post(controller.host, controller.port, "/voice/speak",
                     {"text": "Vantage is online."})
        except Exception as exc:
            messagebox.showerror("Speak", f"Speech failed:\n{exc}")

    def do_restart():
        status.configure(text="restarting…", fg=MUTED)
        dot.configure(fg=MUTED)
        root.update_idletasks()
        controller.restart()
        if controller.wait_ready():
            refresh()
        else:
            show_failure()

    def do_quit():
        controller.stop()
        on_quit()
        root.destroy()

    # Tk does not signal itself when the system asks it to quit -- `tell application
    # "Vantage" to quit`, the Quit menu and Cmd-Q all invoke Tcl's `exit`, which leaves
    # the interpreter from C. Python signal handlers and atexit never run on that path,
    # so the command itself has to be the one that cleans up.
    root.createcommand("exit", lambda *args: do_quit())

    make_button(buttons, "Open Dashboard", open_dashboard, primary=True).pack(side="left")
    make_button(buttons, "Voice Status", voice_status).pack(side="left", padx=8)
    make_button(buttons, "Speak", speak_test).pack(side="left")
    make_button(buttons, "Restart", do_restart).pack(side="left", padx=8)
    make_button(buttons, "Quit", do_quit).pack(side="right")

    # --- devices on your wifi ----------------------------------------------
    # The switch lives here, in a window the user already owns, and nowhere in the HTTP
    # API. The phone is served the same dashboard as the laptop, so a settings endpoint
    # that accepted `lan_mode` would let a connected device open remote access to the
    # machine it is talking to.
    lan_row = tk.Frame(root, bg=BG)
    lan_row.pack(fill="x", padx=16, pady=(0, 6))
    lan_var = tk.BooleanVar(value=lan_switch_state())
    tk.Checkbutton(
        lan_row,
        text="Allow devices on my wifi to connect",
        variable=lan_var,
        command=lambda: toggle_lan(lan_var),
        bg=BG, fg=INK, activebackground=BG, activeforeground=INK,
        selectcolor=PANEL, relief="flat", highlightthickness=0,
        font=("Helvetica Neue", 11), cursor="hand2",
    ).pack(side="left")
    lan_hint = tk.Label(lan_row, text="", bg=BG, fg=MUTED, font=("Helvetica Neue", 10))
    lan_hint.pack(side="left", padx=8)

    # --- what the phone types (or scans) ------------------------------------
    pair_row = tk.Frame(root, bg=BG)
    pair_row.pack(fill="x", padx=16, pady=(0, 8))
    pair_text = tk.Label(pair_row, text="", bg=BG, fg=MUTED,
                         font=("Menlo", 11), justify="left")
    pair_text.pack(side="left")
    pair_image = tk.Label(pair_row, bg=BG)          # the scan code, when it can be drawn
    _keep = {}          # Tk frees an image with no Python reference; this holds one

    def show_pairing(urls):
        target = lan_phone_url(urls)
        if not target:
            pair_text.configure(text="phone: not open to other devices yet", fg=MUTED)
            pair_image.configure(image="")
            return
        pair_text.configure(text=f"phone: {target}", fg=INK)
        png = pairing_qr_png(target)
        if png is None:
            pair_image.configure(image="")
            return
        if _keep.get("url") != target:
            import base64

            # master= matters: PhotoImage otherwise binds to the first interpreter in
            # the process, and an image from another interpreter cannot be shown here.
            _keep["photo"] = tk.PhotoImage(
                master=pair_image, data=base64.b64encode(png).decode("ascii"))
            _keep["url"] = target
        pair_image.configure(image=_keep["photo"])
        pair_image.pack(side="left", padx=(12, 0))

    def toggle_lan(var):
        enabled = var.get()
        try:
            set_lan_switch(enabled)
        except OSError as exc:
            var.set(not enabled)
            messagebox.showerror("Devices on your wifi",
                                 f"Could not save the setting:\n{exc}")
            return
        # The server reads the switch when it starts, so applying it means restarting --
        # and it still refuses a LAN mode with no token, which must be shown rather than
        # swallowed: a checkbox that appears to do nothing is worse than one that says no.
        controller.bind_host = "0.0.0.0" if enabled else "127.0.0.1"
        status.configure(text="restarting…", fg=MUTED)
        dot.configure(fg=MUTED)
        root.update_idletasks()
        controller.restart()
        if controller.wait_ready():
            refresh()
            lan_hint.configure(text="" if not enabled else
                               "phone: open the printed address on the device",
                               fg=MUTED)
            if enabled:
                messagebox.showinfo(
                    "Devices on your wifi",
                    "Other devices on this network can now reach Vantage.\n\n"
                    "Open http://<this laptop's address>:%s/phone on the phone -- the "
                    "exact address is in the log below.\n\nNothing runs without your "
                    "confirmation." % controller.port)
        else:
            var.set(False)
            set_lan_switch(False)
            controller.bind_host = "127.0.0.1"
            controller.restart()
            lan_hint.configure(text="refused: the laptop needs an API token before "
                                    "another device can reach it", fg="#b91c1c")
            show_failure()


    # --- log ---------------------------------------------------------------
    tk.Label(root, text="RECENT ACTIVITY", bg=BG, fg=MUTED,
             font=("Helvetica Neue", 9, "bold")).pack(anchor="w", padx=18)

    log_box = scrolledtext.ScrolledText(
        root, height=9, bg="#0f172a", fg="#e6edf7", insertbackground="#e6edf7",
        font=("Menlo", 10), relief="flat", wrap="word",
    )
    log_box.pack(fill="both", expand=True, padx=14, pady=(4, 14))
    log_box.configure(state="disabled")

    # --- behaviour ---------------------------------------------------------
    def show_failure():
        status.configure(text="failed to start", fg=BAD)
        dot.configure(fg=BAD)
        tail = "\n".join(controller.tail[-18:]) or "(no output)"
        messagebox.showerror(
            "Vantage could not start",
            f"The server did not become ready.\n\nLast output:\n{tail}",
        )

    def refresh():
        if not controller.running():
            status.configure(text="stopped", fg=BAD)
            dot.configure(fg=BAD)
            return

        try:
            health = api_get(controller.host, controller.port, "/health")
            devices = api_get(controller.host, controller.port, "/devices")
            audit = api_get(controller.host, controller.port, "/audit?limit=6")
        except Exception:
            status.configure(text="starting…", fg=MUTED)
            dot.configure(fg=MUTED)
            root.after(900, refresh)
            return

        online = sum(1 for d in devices if d.get("online"))
        stat_labels["devices"].configure(text=f"{online}/{len(devices)}")
        stat_labels["tools"].configure(text=str(health["tools"]))
        pending = health["pending_confirmations"]
        stat_labels["pending"].configure(text=str(pending), fg=BAD if pending else INK)
        stat_labels["llm"].configure(text="cloud" if health["llm_enabled"] else "rules")
        show_pairing(health.get("phone_urls") or [])

        status.configure(
            text=f"running  ·  http://{controller.host}:{controller.port}", fg=OK
        )
        dot.configure(fg=OK)

        lines = list(controller.tail[-40:])
        for row in reversed(audit):
            lines.append(
                f"{row['created_at'][11:19]}  {row['result']:<10}  {row['tool'] or row['intent']}"
            )
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.insert("end", "\n".join(lines[-60:]))
        log_box.see("end")
        log_box.configure(state="disabled")

        root.after(2500, refresh)

    root.protocol("WM_DELETE_WINDOW", do_quit)

    def boot():
        if controller.wait_ready():
            refresh()
            webbrowser.open(f"http://{controller.host}:{controller.port}/dashboard")
        else:
            show_failure()

    root.after(200, boot)
    root.mainloop()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def install_shutdown_handlers(controller: ServerController) -> None:
    """Stop the server on every exit path, not only the Quit button.

    Measured on the delivered app: `tell application "Vantage" to quit` signals the panel,
    the panel dies on its default handler, and the server kept answering over the wifi
    address with no window left to close it. Force Quit, logout and `shutdown` take the
    same route, so the Quit button cannot be the only cleanup.
    """
    import signal

    def handler(number, frame):  # noqa: ARG001
        controller.stop()
        raise SystemExit(0)

    for number in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass  # not the main thread; nothing to do


def run_headless(controller: ServerController, reason: str) -> int:
    """Run the server without a control window.

    The control panel is a convenience; the server is the product. If a window
    cannot be created we still bring Vantage up, open the dashboard, and say
    plainly why there is no window.
    """
    url = f"http://{controller.host}:{controller.port}/dashboard"
    print(f"Vantage: control window unavailable ({reason})")
    print(f"Vantage: running headless -> {url}")

    if not controller.wait_ready():
        _fatal("Vantage could not start its server. See ~/.jarvis-forge/app.log")
        controller.stop()
        return 1

    webbrowser.open(url)
    _notify("Vantage is running", f"The dashboard is open in your browser.\n{url}")

    try:
        # Stay alive until interrupted so the server keeps running.
        while controller.running():
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


def _notify(title: str, message: str) -> None:
    """Best-effort native notification; never fatal."""
    if platform.system() != "Darwin":
        return
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_message}" with title "{safe_title}"'],
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def project_dir_check(controller: "ServerController") -> tuple:
    """The report's first row, including *why* it failed.

    A bundle that cannot find its source has to say more than "no such directory" --
    the pointer is baked in at build time, so the reader needs the fix, not the path.
    """
    error = getattr(controller, "project_dir_error", "")
    ok = bool(controller.project_dir.is_dir()) and not error
    return ("project directory", ok, error or str(controller.project_dir))


def server_is_reachable(controller: "ServerController") -> bool:
    """Whether waiting for the server is a question that can have an answer.

    When the project directory could not be located nothing was ever started, and the
    report used to poll a dead port for 40 seconds before saying so.
    """
    return not getattr(controller, "project_dir_error", "")


def gui_checks() -> list:
    """Construct the widgets the control panel uses.

    A missing Tcl module should surface here rather than at the first click.
    """
    prepare_tk_environment()
    try:
        import tkinter as tk
        from tkinter import scrolledtext  # noqa: F401

        root = tk.Tk()
        root.withdraw()

        frame = tk.Frame(root)
        tk.Label(frame, text="probe")
        tk.Button(frame, text="probe")
        box = scrolledtext.ScrolledText(frame, height=2)
        frame.pack()
        box.pack()
        root.update_idletasks()
        widget_count = len(frame.winfo_children()) + 1
        patchlevel = root.tk.call("info", "patchlevel")
        root.destroy()

        return [("tkinter / Tk", True, f"Tk {patchlevel}"),
                ("control panel widgets", True, f"{widget_count} widgets constructed")]
    except Exception as exc:
        return [("tkinter / Tk", False, f"{type(exc).__name__}: {exc}")]


def selftest(controller: ServerController) -> int:
    """Verify every piece the app needs, and print a report.

    Run with:  JARVIS_SELFTEST=1 "<app>/Contents/MacOS/JarvisForge"
    """
    checks: list[tuple[str, bool, str]] = []
    # Checks the report could not run at all. Naming them keeps "3/5" from reading
    # like a smaller test suite: the denominator shrank because the server never
    # started, not because four capabilities turned out fine.
    not_attempted: list[str] = []

    # --- environment --------------------------------------------------------
    checks.append(project_dir_check(controller))
    checks.append(("python interpreter", Path(controller.python).exists(), controller.python))

    # --- Tk / GUI -----------------------------------------------------------
    checks.extend(gui_checks())

    # --- server -------------------------------------------------------------
    if not server_is_reachable(controller):
        checks.append(("server", False, "not started: " + controller.project_dir_error))
        not_attempted += ["text to speech", "speech to text", "microphone"]
    elif controller.wait_ready(timeout=40):
        try:
            health = api_get(controller.host, controller.port, "/health")
            checks.append(
                ("server", True,
                 f"{health['tools']} tools, agents {health['agents']}, "
                 f"model={'cloud' if health['llm_enabled'] else 'rules'}")
            )
        except Exception as exc:
            checks.append(("server", False, str(exc)))

        try:
            voice = settled_voice_status(controller.host, controller.port)
            tts = [k for k, v in voice["tts"]["available"].items() if v]
            stt = [k for k, v in voice["stt"]["available"].items() if v]
            checks.append(("text to speech", bool(tts), ", ".join(tts) or "none available"))
            checks.append(("speech to text", bool(stt), ", ".join(stt) or "none available"))
            checks.append(
                ("microphone", bool(voice["audio"]["capture_available"]),
                 mic_note(voice["audio"]))
            )
        except Exception as exc:
            checks.append(("voice subsystem", False, str(exc)))
    else:
        checks.append(("server", False, "did not become ready within 40s"))
        not_attempted += ["text to speech", "speech to text", "microphone"]

    # --- speech recognition (TCC) ------------------------------------------
    # Apple only grants Speech Recognition to a bundled, signed app. This probe
    # reports what the *current* install can do. It never mutates state, so a
    # diagnostic run can't change what it is measuring.
    if controller.project_dir.joinpath("jarvis").is_dir():
        try:
            import shutil as _shutil

            if _shutil.which("swiftc") is None:
                checks.append(("speech recognition", False, "swiftc not installed"))
            else:
                sys.path.insert(0, str(controller.project_dir))
                from jarvis.voice.stt import MacOSSpeechSTT
                from jarvis.voice.tts import MacOSSayTTS

                native = MacOSSpeechSTT()
                if native._blocked_marker.exists():
                    checks.append(
                        ("speech recognition", False,
                         "marked blocked by an earlier TCC abort; delete "
                         f"{native._blocked_marker} to retry from a bundled app")
                    )
                elif not native.available():
                    checks.append(("speech recognition", False, "helper not buildable"))
                else:
                    import asyncio as _asyncio

                    clip = MacOSSayTTS()._default_path()
                    _asyncio.run(MacOSSayTTS().synthesize("open vs code", clip))
                    result = _asyncio.run(native.transcribe(clip))

                    if result.ok:
                        checks.append(
                            ("speech recognition", True,
                             f"native macOS STT works (heard: {result.text!r})")
                        )
                    else:
                        checks.append(
                            ("speech recognition", False, result.error.split(".")[0][:90])
                        )
        except Exception as exc:
            checks.append(("speech recognition", False, f"{type(exc).__name__}: {exc}"))

    # --- report -------------------------------------------------------------
    print("\nVantage self-test")
    print("=" * 62)
    for name, ok, detail in checks:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name:<24} {detail}")
    print("=" * 62)

    failures = [name for name, ok, _ in checks if not ok]
    print(f"  {len(checks) - len(failures)}/{len(checks)} checks passed")
    if failures:
        print(f"  failing: {', '.join(failures)}")
    if not_attempted:
        print(f"  not attempted: {', '.join(not_attempted)}")

    return 1 if failures else 0


def main() -> int:
    host = os.environ.get("JARVIS_HOST", "127.0.0.1")
    port = int(os.environ.get("JARVIS_PORT", "8765"))
    log_path = Path(os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis-forge"))) / "app.log"

    if os.environ.get("JARVIS_SELFTEST") == "1":
        # The self-test is what someone reaches for *because* the app will not start, so
        # it cannot sit behind the step that fails first. It used to: resolution raised,
        # an alert was shown, and main() returned 1 without ever running the nine checks
        # that would have named the cause (Phase 9's P0-D3, masked ever since because a
        # working install cannot see it).
        error = ""
        try:
            project_dir = resolve_project_dir()
        except RuntimeError as exc:
            error, project_dir = str(exc), BUNDLE_ROOT
        controller = ServerController(project_dir, resolve_python(project_dir),
                                      host, port, log_path)
        controller.project_dir_error = error
        if not error:
            controller.start()
        try:
            return selftest(controller)
        finally:
            controller.stop()

    try:
        project_dir = resolve_project_dir()
    except RuntimeError as exc:
        _fatal(str(exc))
        return 1

    python = resolve_python(project_dir)
    controller = ServerController(project_dir, python, host, port, log_path)

    controller.start()
    install_shutdown_handlers(controller)

    def on_quit():
        pass

    if os.environ.get("JARVIS_HEADLESS") == "1":
        return run_headless(controller, "JARVIS_HEADLESS=1")

    try:
        build_ui(controller, on_quit)
    except GuiUnavailable as exc:
        # Fall back rather than dying -- the server is what matters.
        return run_headless(controller, str(exc))
    except Exception:
        controller.stop()
        raise
    return 0


def _fatal(message: str) -> None:
    """Show a native alert, falling back to stderr."""
    print(f"Vantage: {message}", file=sys.stderr)
    if platform.system() == "Darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{message}" with title "Vantage" buttons {{"OK"}} '
                 "with icon caution"],
                check=False,
            )
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
