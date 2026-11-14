# Setting up Vantage on another machine

The goal this document serves: **finish on one laptop, `git push`, pull on the other,
keep working from exactly where you left off.** The repository carries the source; a
few things are rebuilt or re-created per machine because they are either large build
outputs, machine-specific, or secret. This page says which is which.

---

## 0. What is and is not in the repository

| In git                                                                             | Not in git — rebuilt or re-created per machine                                   |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| All Python source (`jarvis/`), dashboard (`dashboard/`), desktop launcher (`app/`) | `.venv/` — the virtualenv (recreate it)                                          |
| `requirements.txt`, `pyproject.toml`, `uv.lock`                                    | `dist/`, `app/build/` — desktop bundle output                                    |
| Tests (`tests/`) and benchmarks                                                    | `macos/Vantage/.build/`, `android/app/build/`, `ios/**/.build/` — compile output |
| macOS/iOS/Android **source** (`macos/Vantage/Sources`, `android/app/src`, …)       | `macos/Vantage/Vantage.app` — the built, signed bundle                           |
| `dashboard/` pages and `assets/`                                                   | `.env` — your real configuration (only `.env.example` is committed)              |
| Docs (`docs/`, `README.md`) and the audit trail (`audit/`)                         | `~/.jarvis-forge/` — token, local calendar/mail DB, audit chain, voice dir       |

Because the data directory is out of tree, a fresh clone starts **empty** — no token, no
calendar, no mail. That is intentional and safe. If you want to carry data across,
see [§7 Carrying your data](#7-carrying-your-data-across-machines).

---

## 1. Prerequisites

- **macOS** (the product drives `say`, `pmset`, AppleScript, the Vision framework).
- **Python 3.10–3.12** (tested on 3.11 and 3.12). **Pin the version explicitly** —
  `uv venv` otherwise picks the newest interpreter on your machine, and a newer
  Python links a newer OpenSSL. (This was a real break: a certificate the gateway
  generates had no `CA:TRUE`, which OpenSSL 3.x rejects but the older one accepted;
  it is fixed, and `tests/test_companion_cert_is_a_valid_ca.py` now pins it. Using
  a supported interpreter is still the safe default.)
- **[uv](https://docs.astral.sh/uv/)** (recommended) — the project's venv is uv-managed
  and has **no `pip` inside it**. Plain `python -m venv` + `pip` also works.
- **Node.js** — only for the tests that execute the dashboard's own JavaScript.
- Optional: **Xcode** (native macOS app + `swift test`), **JDK 17 + Android SDK**
  (Android companion), **ffmpeg** (`brew install ffmpeg`) for the voice capture tests.

---

## 2. Clone

```bash
git clone https://github.com/abhijeet1267/jarvis-forge.git
cd jarvis-forge
git branch --show-current        # should print: main
```

---

## 3. Open in VS Code

```bash
code .                           # or: code /path/to/jarvis-forge
```

Select the interpreter so VS Code uses the project venv: **Cmd+Shift+P → “Python:
Select Interpreter” → `./.venv/bin/python`**. Nothing else in the repo requires
editor setup; `.venv/`, build dirs and `.env` are already ignored.

---

## 4. Install dependencies

**Recommended (uv):**

```bash
uv venv --python 3.11 .venv     # pin: do not let uv pick 3.13+
uv pip install --python .venv/bin/python -r requirements.txt
```

**Alternatively (plain venv + pip):**

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Optional pieces, when you need them:

```bash
# Browser-driven dashboard tests
.venv/bin/python -m playwright install chromium

# macOS desktop bundle (needs Pillow, already in requirements.txt)
.venv/bin/python build_app.py --dest ./dist

# Native macOS app
swift build -C macos/Vantage

# Native Android companion (JDK 17 + ANDROID_HOME required)
cd android && ./gradlew :app:assembleDebug && cd ..
```

> **Do not** copy the `.venv/` from the other laptop. Paths are absolute and the
> interpreter is machine-specific. Recreate it here.

---

## 5. Configure environment variables

```bash
cp .env.example .env
```

`.env.example` documents **every** `JARVIS_*` setting with blank or safe defaults. The
app runs with an empty `.env` — no LLM key, no network, no vendor. Fill in only what you
use:

- **LLM (optional):** `JARVIS_AI_PROVIDER`, `JARVIS_LLM_BASE_URL`, `JARVIS_LLM_API_KEY`,
  `JARVIS_LLM_MODEL`.
- **Google Calendar/Tasks (optional):** `JARVIS_GOOGLE_CLIENT_ID` +
  `JARVIS_GOOGLE_CLIENT_SECRET` from an OAuth **Desktop app** client. The refresh token
  is stored in the **macOS keychain**, not in `.env`.
- **Email (optional):** `JARVIS_EMAIL_*`; leave blank to use the local maildir.
- **n8n / MCP / Figma (optional):** see the sections in `.env.example`.

Two things worth knowing:

- **List settings are JSON arrays**, not comma strings:
  `JARVIS_ALLOWED_COMMANDS=["ls","cat"]`. A comma string fails at startup — a real bug
  that shipped and is now pinned by `tests/test_env_example_is_loadable.py`.
- **Never commit `.env`.** It is gitignored, and so is every `.env.*` backup.

`JARVIS_HOME` defaults to `~/.jarvis-forge`; override it to run against a different data
directory (handy for experiments).

---

## 6. Start it, and run the checks

```bash
# API + dashboard
.venv/bin/python run.py
#   API  http://127.0.0.1:8765
#   UI   http://127.0.0.1:8765/dashboard
#   auth token is created on first run at ~/.jarvis-forge/api_token
```

**If the server refuses to start** with "LAN mode is on but the server binds to
127.0.0.1": the data directory you carried over (or an earlier session) left
`~/.jarvis-forge/lan_mode` set to `true`. Either match it —
`JARVIS_HOST=0.0.0.0 .venv/bin/python run.py` — or turn LAN mode off in the app.
The guard is deliberate: opening the network door must be an explicit choice.

Other entry points: `python run.py cli` (text REPL), `python run.py voice` (voice REPL),
`python run.py demo` (runs the acceptance commands).

Run the suite before trusting a change. The OCR/browser-heavy files are flaky under load
on some machines — run them separately and uncontended:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
# If the OCR/browser tail is unstable here, exclude it and run it alone:
.venv/bin/python -m pytest -q -p no:cacheprovider \
  --ignore=tests/test_vision.py --ignore=tests/test_attachment_ocr_text.py \
  --ignore=tests/test_attachment_client_contract.py \
  --ignore=tests/test_macos_attachment_client.py \
  --ignore=tests/test_dashboard_browser_flows.py
.venv/bin/python -m pytest tests/test_vision.py tests/test_attachment_ocr_text.py \
  tests/test_attachment_client_contract.py tests/test_macos_attachment_client.py \
  tests/test_dashboard_browser_flows.py -q -p no:cacheprovider
```

Some tests pin the **live** backend on `127.0.0.1:8765` (they read the real `/health`
shape). Start `run.py` first, or those files fail for lack of a server, not for a code
defect. The dashboard browser tests, by contrast, spin up their **own** isolated server
on a scratch home and never touch your real data.

---

## 7. Carrying your data across machines

The runtime directory `~/.jarvis-forge/` holds your token, local calendar/mail SQLite
databases, the audit chain and the voice directory. It is **not** in git. To move it:

```bash
# on the old laptop
tar -C ~ -czf jarvis-forge-data.tgz .jarvis-forge

# transfer jarvis-forge-data.tgz however you like (dropbox, scp, usb), then on the new one
tar -C ~ -xzf jarvis-forge-data.tgz
```

Then start the app; it reuses the existing token and databases. **Treat this archive as a
secret** — it contains the API token. Do not commit it or upload it anywhere public. If
you would rather start clean, skip this and let first-run create a fresh directory.

---

## 8. Daily workflow across two laptops

```bash
# start of a session
git pull --ff-only

# ... work ...

# before switching machines
git add -A && git status          # review what changed
git commit -m "describe the change"
git push

# on the other laptop
git pull --ff-only
```

Rules that keep this clean:

- `pull --ff-only` will **refuse** rather than silently merge if the branches diverged;
  that is the point — resolve before continuing.
- Build outputs, the venv and `.env` never appear in `git status` (they are ignored).
- If a change needs a new dependency, update `requirements.txt` (and `uv.lock`) so the
  other laptop installs the same thing.

---

## 9. Troubleshooting

| Symptom                                                                        | Cause and fix                                                                                                                                                                     |
| ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SettingsError ... allowed_commands` at startup                                | A list setting is a comma string. Use a JSON array: `["ls","cat"]`.                                                                                                               |
| `ModuleNotFoundError`                                                          | The venv is not active or missing a dependency. Reinstall from `requirements.txt`; select `./.venv/bin/python` in VS Code.                                                        |
| `pip: command not found`                                                       | The uv venv has no pip. Use `uv pip install --python .venv/bin/python ...` or a plain venv.                                                                                       |
| Backend refuses to start: “LAN mode is on but the server binds to 127.0.0.1”   | Deliberate guard. Start with `JARVIS_HOST=0.0.0.0` (and `JARVIS_LAN_MODE=true`) or turn LAN mode off.                                                                             |
| Tests fail with connection refused on `:8765`                                  | Those tests pin the live backend. Start `run.py` first.                                                                                                                           |
| Browser tests fail with `ERR_CONNECTION_REFUSED`                               | Same: they need a server; they start their own, but Playwright/Chromium must be installed (`python -m playwright install chromium`).                                              |
| `swift test` fails: `no such module 'XCTest'`                                  | Only Command Line Tools are installed. Install full Xcode.                                                                                                                        |
| Android build fails on the default JDK                                         | Use JDK 17: `export JAVA_HOME=$(/usr/libexec/java_home -v 17)` and `export ANDROID_HOME=$HOME/Library/Android/sdk`.                                                               |
| OCR/vision tests time out                                                      | They are CPU-sensitive. Run them alone, not beside a build or another suite.                                                                                                      |
| Companion gateway: “no healthy response within 15s” / `invalid CA certificate` | The generated cert was not marked a CA (fixed 2026-11-14). Delete `~/.jarvis-forge/companion/companion.{crt,key}` so a correct one is regenerated, and run on a supported Python. |
