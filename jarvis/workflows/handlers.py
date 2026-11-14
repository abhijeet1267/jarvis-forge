"""Real macOS Workflow Execution Handlers (Phase 77).

Actual implementations for workflow steps:
- Application control via subprocess/AppleScript
- File operations with safety checks
- Controlled shell execution
- Browser automation primitives

All handlers use approved mechanisms with verification before reporting success.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable


@dataclass
class AppControlResult:
    """Result from application control operation."""
    success: bool
    application: str
    was_running: bool = False
    message: str = ""


# Handler registry (populated after classes are defined)
DEFAULT_HANDLERS: Dict[str, Callable] = {
    'open_application': lambda args: ApplicationHandler.open_application(args.get('application', '') if args else ''),
    'quit_application': lambda args: ApplicationHandler.quit_application(args.get('application', '') if args else ''),
    'focus_application': lambda args: ApplicationHandler.focus_application(args.get('application', '') if args else ''),
    'list_applications': lambda args: ApplicationHandler.list_applications(),  # No args needed
    'check_process_exists': lambda args: {
        # Asking whether an app is running is a question, not a command: the
        # step succeeds when it gets an answer. Returning `success: False` for
        # an app that is simply not running made the step read as a failure and
        # the engine report the unhelpful "reported failure without a reason".
        "success": True,
        "application": args.get('application', ''),
        "running": ApplicationHandler._is_running(args.get('application', '')),
    },
    'create_directory': lambda args: FileControlHandler.create_directory(args.get('name', ''), args.get('path')),
    'delete_file': lambda args: FileControlHandler.delete_file(args.get('path', '')),
    'move_file': lambda args: FileControlHandler.move_file(args.get('source', ''), args.get('destination', '')),
    'search_files': lambda args: FileControlHandler.search_files(
        args.get('query', ''), args.get('path'), args.get('timeout', 30)),
    'execute_shell': lambda args: ShellExecutor.execute_command(
        args.get('command', ''), args.get('working_dir'),
        timeout=args.get('timeout', 30), env=args.get('env')),
    'detect_browser': lambda args: BrowserControlHandler.detect_browser(),  # No args needed
    'navigate_url': lambda args: BrowserControlHandler.navigate_url(args.get('url', '')),
    'close_browser': lambda args: BrowserControlHandler.close_browser(args.get('browser', '')),
}


# Finalize DEFAULT_HANDLERS after imports
import sys
if __name__ == '__main__' or 'ApplicationHandler' in globals() and 'FileControlHandler' in globals():
    # Registry already initialized above
    pass


class ApplicationHandler:
    """Handle macOS application control."""
    
    @staticmethod
    def open_application(name: str) -> Dict[str, Any]:
        """Launch an application by name or bundle ID."""
        
        # Normalize application name
        normalized = ApplicationHandler._normalize_app_name(name)
        
        try:
            cmd = ['open', '-a', normalized]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            # `open` returns non-zero when it could not launch the app, and a
            # workflow step that says "Application opened" about that is the
            # false success this project refuses. Success follows the exit code.
            opened = result.returncode == 0
            return {
                "success": opened,
                "application": normalized,
                "was_running": opened and ApplicationHandler._is_running(normalized),
                "message": "Application opened" if opened
                           else f"Failed: {result.stderr.strip() or 'open exited ' + str(result.returncode)}"
            }
            
        except subprocess.TimeoutExpired:
            return {"success": False, "application": normalized, "message": "Command timed out"}
        except FileNotFoundError:
            return {"success": False, "application": normalized, "message": f"Application not found: {name}"}
        except Exception as e:
            return {"success": False, "application": normalized, "message": str(e)}
    
    @staticmethod
    def _is_running(app_name: str) -> bool:
        """Check if application is currently running.

        Matching is by exact name, not by "contains". A substring match reported
        Cursor as running on a machine where it is not installed, because macOS
        ships ``CursorUIViewService`` (a text-input service) and its path contains
        "cursor" -- which then made END_WORKDAY fail with a misleading message and
        made OPEN_WORKSPACE claim a launch it had not performed.
        """
        try:
            # Try using osascript to get running apps
            script = f'''
            tell application "System Events"
                exists (every application process whose name is "{app_name}")
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                return "true" in result.stdout.lower()
        except:
            pass
        
        # Fallback: check process list
        return ApplicationHandler._process_exists(app_name)
    
    @staticmethod
    def _process_exists(process_name: str) -> bool:
        """Whether a process with this executable name is running.

        Compared against the executable's basename, not as a substring of the
        whole path. ``ps`` prints full paths, so a substring test matched any
        process whose *directory* happened to contain the name -- the macOS
        ``.../CursorUIViewService`` helper made "Cursor" look installed and
        running.
        """
        try:
            result = subprocess.run(
                ["ps", "-wwo", "comm=", "-A"],
                capture_output=True,
                text=True,
                timeout=5
            )
        except Exception:
            return False
        wanted = process_name.strip().lower()
        if not wanted:
            return False
        for line in result.stdout.splitlines():
            name = Path(line.strip()).name.lower()
            if name == wanted or name == wanted + ".app":
                return True
        return False
    
    @staticmethod
    def _normalize_app_name(name: str) -> str:
        """Normalize application name for macOS."""
        # Remove common suffixes/prefixes
        name = name.strip()
        
        # Handle common variations
        variations = {
            "visual studio code": "Visual Studio Code",
            "vs code": "Visual Studio Code",
            "atom": "Atom",
            "sublime text": "Sublime Text",
            "textmate": "TextMate",
            "terminal": "Terminal",
            "iterm2": "iTerm2",
            "safari": "Safari",
            "chrome": "Google Chrome",
            "firefox": "Firefox",
            "browser": "Safari",  # Default fallback
        }
        
        return variations.get(name.lower(), name.title())
    
    @staticmethod
    def quit_application(name: str) -> Dict[str, Any]:
        """Quit an application.

        "It is not running" is the desired end state, not a failure — reporting it
        as one made END_WORKDAY fail on a machine where there was nothing to quit.
        ``was_running`` was also hardcoded True without ever being checked.
        """
        normalized = ApplicationHandler._normalize_app_name(name)

        if not ApplicationHandler._is_running(normalized):
            return {
                "success": True,
                "application": normalized,
                "was_running": False,
                "message": f"{normalized} was not running",
            }

        try:
            script = f'''
            tell application "{normalized}"
                quit
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            # Wait a moment then check if it's gone
            import time
            time.sleep(2)
            
            still_running = ApplicationHandler._is_running(normalized)
            
            return {
                "success": not still_running,
                "application": normalized,
                "was_running": True,
                "message": "Application quit successfully" if not still_running
                           else f"{normalized} is still running after quit was requested",
            }
            
        except subprocess.TimeoutExpired:
            return {"success": False, "application": normalized, "was_running": False, "message": "Timeout waiting for quit"}
        except Exception as e:
            return {"success": False, "application": normalized, "was_running": False, "message": str(e)}
    
    @staticmethod
    def list_applications() -> Dict[str, Any]:
        """List all running applications."""
        try:
            cmd = ['osascript', '-e', 
                   'tell application "System Events" to get name of every process']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                apps = [app.strip() for app in result.stdout.strip().split('\n') if app.strip()]
            else:
                apps = []
            
            # The engine decides pass/fail from `success`; omitting it made a
            # working listing read as a failed step.
            return {
                "success": result.returncode == 0,
                "applications": [{"name": app} for app in apps],
                "count": len(apps),
                "error": "" if result.returncode == 0 else result.stderr.strip(),
            }

        except Exception as e:
            return {"success": False, "applications": [], "error": str(e)}
    
    @staticmethod
    def focus_application(name: str) -> Dict[str, Any]:
        """Bring application to foreground."""
        normalized = ApplicationHandler._normalize_app_name(name)
        
        try:
            script = f'''
            tell application "{normalized}"
                activate
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            return {
                "success": result.returncode == 0,
                "application": normalized,
                "message": "Application activated" if result.returncode == 0 else f"Failed: {result.stderr.strip()}"
            }
            
        except Exception as e:
            return {"success": False, "application": normalized, "message": str(e)}


class FileControlHandler:
    """Handle file system operations with safety."""
    
    PROTECTED_PATHS = [
        '/System', '/usr/bin', '/usr/local/var', '/kernel',
        '/var/db', '/etc/passwd', '/etc/shadow'
    ]
    
    @staticmethod
    def create_directory(name: str, path: Optional[str] = None) -> Dict[str, Any]:
        """Create a directory with safety checks."""
        try:
            full_path = Path(path) / name if path else Path(name)
            
            # Check protection
            if FileControlHandler._is_protected(full_path):
                return {"success": False, "blocked": True, "reason": "Protected path"}
            
            full_path.mkdir(parents=True, exist_ok=True)
            
            return {
                "success": full_path.exists(),
                "created_path": str(full_path.absolute()),
                "exists": full_path.exists()
            }
        
        except PermissionError:
            return {"success": False, "blocked": True, "reason": "Permission denied"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def delete_file(path: str) -> Dict[str, Any]:
        """Delete a file with high-risk policy."""
        try:
            file_path = Path(path)
            
            # Resolve to absolute path first
            try:
                file_path = file_path.resolve(strict=True)
            except FileNotFoundError:
                return {"success": True, "deleted_path": str(path), "already_absent": True}
            
            # Check protection
            if FileControlHandler._is_protected(file_path):
                return {"success": False, "blocked": True, "reason": "Protected path"}
            
            # Delete based on type
            if file_path.is_dir():
                shutil.rmtree(file_path)
            else:
                file_path.unlink()
            
            # Verify deletion
            still_exists = file_path.exists()
            
            return {
                "success": not still_exists,
                "deleted_path": str(file_path),
                "still_exists": still_exists
            }
        
        except PermissionError:
            return {"success": False, "reason": "Permission denied"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def move_file(source: str, destination: str) -> Dict[str, Any]:
        """Move/rename a file."""
        try:
            src = Path(source).resolve()
            dst = Path(destination)
            
            if FileControlHandler._is_protected(src):
                return {"success": False, "blocked": True, "reason": "Source protected"}
            
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            
            return {
                "success": dst.exists(),
                "moved_from": str(src),
                "moved_to": str(dst.absolute())
            }
        
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def search_files(query: str, path: Optional[str] = None,
                     timeout: int = 30) -> Dict[str, Any]:
        """Search for files.

        Two things were wrong here. ``~`` was passed to ``find`` verbatim, which
        does not expand it, so the CLEAN_DOWNLOADS template searched for a
        directory literally named ``~/Downloads`` and found nothing. And exit 1
        was treated as "no matches" -- but ``find`` also exits 1 when the path
        does not exist, so a missing folder was reported as a successful empty
        search. That is the "empty is not the same as unreadable" mistake this
        project keeps fixing, so a real error now surfaces as one.
        """
        try:
            search_path = Path(path).expanduser() if path else Path.home()

            if not search_path.exists():
                return {"success": False, "results": [], "count": 0,
                        "search_path": str(search_path),
                        "error": f"no such directory: {search_path}"}

            cmd = ['find', str(search_path), '-iname', query, '-type', 'f', '-maxdepth', '10']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            
            if result.returncode == 0:
                files = [f for f in result.stdout.strip().split('\n') if f]
            else:
                files = []
            
            # `find` exits 1 both for "no matches" and for real errors. Only a
            # clean exit is a trustworthy empty; anything on stderr means part
            # of the tree was not read, which must not be reported as "none".
            searched = result.returncode == 0 and not result.stderr.strip()
            return {
                "success": searched,
                "results": files,
                "count": len(files),
                "search_path": str(search_path),
                "error": "" if searched else result.stderr.strip()[:300],
            }

        except Exception as e:
            return {"success": False, "results": [], "error": str(e)}
    
    @staticmethod
    def _is_protected(path: Path) -> bool:
        """Check if path is protected."""
        path_str = str(path.absolute())
        return any(path_str.startswith(p) for p in FileControlHandler.PROTECTED_PATHS)


class ShellExecutor:
    """Execute shell commands with safety controls."""
    
    ALLOWED_COMMANDS = [
        'ls', 'pwd', 'echo', 'cat', 'head', 'tail', 'grep', 'find',
        'git', 'python', 'python3', 'node', 'npm', 'yarn',
        'pytest', 'flake8', 'black', 'isort',
        'brew', 'curl', 'wget', 'diff',
    ]
    
    DANGEROUS_PATTERNS = [
        'rm -rf', 'dd ', 'mkfs', 'chmod 000', 'chown root',
        '> /dev/', '< /dev/', 'sudo rm', 'shutdown', 'reboot',
        'kill -9', 'swapoff', 'swap', 'fsck', 'mount',
    ]
    
    @staticmethod
    def execute_command(command: str, working_dir: Optional[str] = None,
                       timeout: int = 30, safe_mode: bool = True,
                       env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Execute a shell command with safety restrictions.

        ``env`` is a small overlay, not a replacement: it is applied on top of
        the current environment with ``${NAME}`` referring to the inherited
        value, so a step can put a directory first on ``PATH`` without losing
        everything else.
        """
        
        # Safety check
        if safe_mode:
            command_lower = command.lower().strip()
            
            # Check for dangerous patterns
            for pattern in ShellExecutor.DANGEROUS_PATTERNS:
                if pattern in command_lower:
                    return {
                        "success": False,
                        "exit_code": -1,
                        "blocked": True,
                        "reason": f"Dangerous command blocked: {pattern}"
                    }
            
            # Extract first word to check against allowlist
            first_word = command.split()[0] if command.split() else ""
            
            if first_word not in ShellExecutor.ALLOWED_COMMANDS:
                return {
                    "success": False,
                    "exit_code": -1,
                    "blocked": True,
                    "reason": f"Command not in allowlist: {first_word}"
                }
        
        try:
            # Set working directory
            cwd = Path(working_dir) if working_dir else Path.cwd()
            
            child_env = None
            if env:
                import os

                child_env = dict(os.environ)
                for key, value in env.items():
                    child_env[key] = str(value).replace("${" + key + "}",
                                                        str(os.environ.get(key, "")))

            # Run the command in its own process group. `subprocess.run` kills
            # only the shell on timeout; anything the shell started (a test
            # suite, a build) survives as an orphan -- a timed-out RUN_TESTS
            # step left a full pytest run alive, reparented to launchd, and
            # the next attempt stacked another one on top.
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env,
                start_new_session=True,
            )
            
            return {
                "success": result.returncode == 0,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": False
            }
        
        except subprocess.TimeoutExpired as exc:
            # The group leader is the shell; signalling the group takes the
            # whole tree down with it.
            import os
            import signal

            pid = getattr(exc, "pid", None)
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "timed_out": True
            }
        except Exception as e:
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "error": str(e)
            }


class BrowserControlHandler:
    """Safe browser automation controls."""
    
    SUPPORTED_BROWSERS = [
        "Google Chrome",
        "Safari",
        "Mozilla Firefox",
        "Microsoft Edge",
    ]
    
    @staticmethod
    def detect_browser() -> Dict[str, Any]:
        """Detect active browser."""
        try:
            cmd = ['osascript', '-e', 
                   'tell application "System Events" to name of first application process whose name contains "Chrome" or name contains "Safari" or name contains "Firefox"']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                browsers = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
                return {"browser": browsers[0] if browsers else None, "browsers_found": browsers}
            else:
                return {"browser": None, "browsers_found": []}
        
        except Exception as e:
            return {"browser": None, "error": str(e)}
    
    @staticmethod
    def navigate_url(url: str, browser_name: str = "") -> Dict[str, Any]:
        """Navigate browser to URL."""
        
        # If no browser specified, try to detect one
        if not browser_name:
            detection = BrowserControlHandler.detect_browser()
            browser_name = detection.get("browser") or "Safari"
        
        try:
            # Use AppleScript for Safari specifically
            if "Safari" in browser_name:
                script = f'''
                tell application "Safari"
                    set newtab to make new tab at top of windows 1 with properties {{URL:"{url}"}}
                    activate
                end tell
                '''
            elif "Chrome" in browser_name:
                script = f'''
                tell application "Google Chrome"
                    do shell script "open -a GoogleChrome \"{url}\""
                    activate
                end tell
                '''
            else:
                # Generic: use open command
                result = subprocess.run(['open', '-a', browser_name, url], 
                                      capture_output=True, text=True, timeout=10)
                return {
                    "success": result.returncode == 0,
                    "browser": browser_name,
                    "url": url,
                    "message": "Navigation initiated"
                }
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=15
            )
            
            return {
                "success": result.returncode == 0,
                "browser": browser_name,
                "url": url,
                "message": "Navigation successful" if result.returncode == 0 else f"Failed: {result.stderr.strip()}"
            }
            
        except Exception as e:
            return {"success": False, "browser": browser_name, "url": url, "message": str(e)}
    
    @staticmethod
    def close_browser(browser_name: str = "") -> Dict[str, Any]:
        """Close a browser."""
        
        if not browser_name:
            browser_name = "Safari"
        
        return ApplicationHandler.quit_application(browser_name)


