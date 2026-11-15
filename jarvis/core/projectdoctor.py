"""Project Doctor: inspect a real code project and report what is actually wrong.

The whole value here is that the answers are *observed*, not guessed. Every
finding below comes from reading a file that exists, running a command that
returned something, or parsing output that was produced -- never from a model's
opinion of what usually goes wrong in a project that looks like this.

Design rules that keep it honest:

* **Detect by evidence.** A project's type is decided by the files present
  (a `pyproject.toml`, a `package.json`, a `Package.swift`), not by its name or
  by what the user said it was.
* **Only claim a check that ran.** Each diagnostic carries the command it ran and
  the exit code it saw. A check this system does not know how to perform is
  reported as ``not_checked``, never as passing.
* **Read-only by default.** Inspection never modifies the project. Repairs go
  through ParallelWorld, where they are previewed and approved like any other
  file change -- this module only *proposes* them.
* **Bounded execution.** Every command has a timeout and a fixed argument list;
  nothing here builds a shell string from user input.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.projectdoctor")

#: How long any single diagnostic may run. A doctor that hangs on a slow test
#: command is worse than one that says it ran out of time.
COMMAND_TIMEOUT = 60.0


@dataclass
class Finding:
    """One observed fact about a project."""

    check: str
    status: str            # ok | problem | not_checked
    detail: str
    command: str = ""
    exit_code: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"check": self.check, "status": self.status, "detail": self.detail,
                "command": self.command, "exit_code": self.exit_code}


@dataclass
class ProjectProfile:
    kind: str
    root: str
    name: str = ""
    manifest: str = ""
    build_file: str = ""
    test_command: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "root": self.root, "name": self.name,
            "manifest": self.manifest, "build_file": self.build_file,
            "test_command": list(self.test_command),
            "findings": [f.to_dict() for f in self.findings],
            "problems": [f.to_dict() for f in self.findings
                         if f.status == "problem"],
        }


#: The markers that identify each ecosystem, most specific first.
_MARKERS = (
    ("python", "pyproject.toml"),
    ("python", "setup.py"),
    ("python", "requirements.txt"),
    ("node", "package.json"),
    ("swift", "Package.swift"),
    ("rust", "Cargo.toml"),
    ("go", "go.mod"),
    ("gradle", "build.gradle"),
    ("gradle", "build.gradle.kts"),
)


def detect(root: Path) -> Optional[ProjectProfile]:
    """What kind of project is this, decided by which files exist."""
    if not root.is_dir():
        return None
    for kind, marker in _MARKERS:
        candidate = root / marker
        if candidate.is_file():
            profile = ProjectProfile(kind=kind, root=str(root), manifest=marker,
                                     name=root.name)
            profile.test_command = _test_command_for(kind, root)
            return profile
    return None


def _test_command_for(kind: str, root: Path) -> List[str]:
    """The command that would run this project's tests, or an empty list.

    Only commands whose runner is actually present are offered: suggesting
    `pytest` on a machine without it would produce a failure the person would
    read as a problem with their project.
    """
    if kind == "python":
        if shutil.which("pytest") or (root / ".venv" / "bin" / "pytest").exists():
            return ["pytest", "-q"]
        return []
    if kind == "node" and (root / "package.json").is_file():
        try:
            data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        scripts = data.get("scripts") or {}
        if "test" in scripts:
            return ["npm", "test", "--silent"]
        return []
    if kind == "swift":
        return ["swift", "build"] if shutil.which("swift") else []
    if kind == "rust":
        return ["cargo", "check"] if shutil.which("cargo") else []
    if kind == "go":
        return ["go", "build", "./..."] if shutil.which("go") else []
    if kind == "gradle":
        wrapper = root / "gradlew"
        if wrapper.is_file():
            return ["./gradlew", "compileJava", "--console=plain"]
        return []
    return []


def _run(command: List[str], root: Path, timeout: float = COMMAND_TIMEOUT
         ) -> tuple[int, str]:
    """Run one permit-listed command in the project, returning (code, output)."""
    try:
        result = subprocess.run(command, cwd=str(root), capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"the check did not finish within {int(timeout)}s"
    except FileNotFoundError:
        return 127, f"{command[0]} is not installed"
    except OSError as exc:
        return 126, f"could not run {command[0]}: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


def inspect(root_path: str, *, run_checks: bool = False) -> Dict[str, Any]:
    """Inspect a project and report what was actually observed.

    `run_checks` is off by default: reading files is safe, running a build or a
    test suite is a real execution that should be asked for. When it is on, the
    profile's own test command runs with a timeout and its exit code is reported.
    """
    root = Path(root_path).expanduser()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    profile = detect(root)
    if profile is None:
        profile = ProjectProfile(kind="unknown", root=str(root), name=root.name)
        profile.findings.append(Finding(
            "project type", "problem",
            "no recognised project manifest (pyproject.toml, package.json, "
            "Package.swift, Cargo.toml, go.mod or a Gradle build file)"))
        return profile.to_dict()

    profile.findings.append(Finding(
        "project type", "ok", f"{profile.kind} project, detected by {profile.manifest}"))

    # Git state, if this is a repository.
    if (root / ".git").exists() and shutil.which("git"):
        code, output = _run(["git", "status", "--porcelain"], root)
        if code == 0:
            changed = [line for line in output.splitlines() if line.strip()]
            profile.findings.append(Finding(
                "git", "ok" if not changed else "ok",
                f"{len(changed)} uncommitted change(s)" if changed else "clean",
                command="git status --porcelain", exit_code=code))
        else:
            profile.findings.append(Finding(
                "git", "not_checked", output[:200] or "git status failed",
                command="git status --porcelain", exit_code=code))

    # Manifest readability: a malformed manifest is a real, common failure.
    manifest_path = root / profile.manifest
    if profile.manifest.endswith(".json"):
        try:
            json.loads(manifest_path.read_text(encoding="utf-8"))
            profile.findings.append(Finding(
                "manifest", "ok", f"{profile.manifest} is valid JSON"))
        except ValueError as exc:
            profile.findings.append(Finding(
                "manifest", "problem",
                f"{profile.manifest} is not valid JSON: {exc}"))
        except OSError as exc:
            profile.findings.append(Finding(
                "manifest", "problem", f"{profile.manifest} could not be read: {exc}"))

    if not profile.test_command:
        profile.findings.append(Finding(
            "tests", "not_checked",
            "no test command is available for this project on this machine"))

    if run_checks and profile.test_command:
        code, output = _run(profile.test_command, root)
        tail = "\n".join(output.splitlines()[-12:])[:800]
        profile.findings.append(Finding(
            "build/tests", "ok" if code == 0 else "problem",
            tail or ("passed" if code == 0 else f"exited {code}"),
            command=" ".join(profile.test_command), exit_code=code))

    return profile.to_dict()


def propose_repair(root_path: str, finding_detail: str = "") -> Dict[str, Any]:
    """A repair proposal, or an honest statement that none is known.

    This proposes; it never applies. A repair is returned in the shape
    ParallelWorld accepts so it can be previewed and approved like any other
    change -- the doctor does not get its own write path.
    """
    root = Path(root_path).expanduser()
    profile = detect(root)
    operations: List[Dict[str, Any]] = []
    summary = ""

    manifest = root / "package.json"
    if profile is not None and manifest.is_file():
        try:
            json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            # A malformed package.json cannot be repaired by guessing its
            # contents; say so rather than inventing a replacement.
            return {
                "operations": [],
                "summary": ("package.json is not valid JSON. This system will not "
                            "guess what it should contain -- restore it from version "
                            "control or rewrite it deliberately."),
                "applicable": False,
            }

    if not operations:
        return {
            "operations": [],
            "summary": (finding_detail or "no automatic repair is known for this "
                        "finding; it needs a person to decide"),
            "applicable": False,
        }

    return {"operations": operations, "summary": summary, "applicable": True}