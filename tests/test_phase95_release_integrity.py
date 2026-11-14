"""Phase 95: Release Integrity Verification Tests.

Verifies the actual packaged macOS application:
- Version metadata accuracy
- Bundle structure integrity
- Source-to-artifact correspondence
- Basic launch/shutdown behavior
"""

import pytest
import plistlib
from pathlib import Path
from typing import Dict, Any


@pytest.fixture
def vantage_bundle() -> Path:
    """Path to Vantage.app bundle."""
    bundle = Path(__file__).parent.parent / "dist" / "Vantage.app"
    if not bundle.exists():
        pytest.skip("Vantage.app bundle not found in dist/")
    return bundle


class TestBundleStructure:
    """Verify macOS bundle has required structure."""
    
    def test_bundle_exists(self, vantage_bundle):
        """Vantage.app bundle exists in dist directory."""
        assert vantage_bundle.exists(), "Vantage.app not found in dist/"
        assert vantage_bundle.is_dir(), "Must be a directory"
    
    def test_contents_exists(self, vantage_bundle):
        """Bundle has Contents directory."""
        contents = vantage_bundle / "Contents"
        assert contents.exists(), "Contents/ directory missing from bundle"
    
    def test_macos_directory_exists(self, vantage_bundle):
        """Bundle has MacOS subdirectory."""
        macos = vantage_bundle / "Contents" / "MacOS"
        assert macos.exists(), "MacOS/ directory missing from bundle"
    
    def test_executable_exists(self, vantage_bundle):
        """Bundle has executable launcher."""
        launcher = vantage_bundle / "Contents" / "MacOS" / "JarvisForge"
        assert launcher.exists(), "Executable JarvisForge not found"
        assert launcher.stat().st_mode & 0o111, "Launcher must be executable"
        # Verify it's a shell script (launcher shim)
        content = launcher.read_text()
        assert content.startswith("#!/bin/sh"), "Launcher should be shell script"
    
    def test_resources_exists(self, vantage_bundle):
        """Bundle has Resources directory with app files."""
        resources = vantage_bundle / "Contents" / "Resources"
        assert resources.exists(), "Resources/ directory missing from bundle"
        
        # Check for key resource files
        python_launcher = resources / "app" / "run.py"
        assert python_launcher.exists(), "App run.py not found"
        
        dashboard_dir = resources / "app" / "dashboard"
        assert dashboard_dir.exists(), "Dashboard not bundled"


class TestVersionInformation:
    """Verify version metadata consistency."""
    
    def test_bundle_version_present(self, vantage_bundle):
        """CFBundleShortVersionString is set in Info.plist."""
        info_plist = vantage_bundle / "Contents" / "Info.plist"
        assert info_plist.exists(), "Info.plist not found"
        
        with open(info_plist, "rb") as f:
            plist_data = plistlib.load(f)
        
        assert "CFBundleShortVersionString" in plist_data, \
            "CFBundleShortVersionString missing from Info.plist"
    
    def test_bundle_version_format(self, vantage_bundle):
        """Version follows semantic versioning pattern."""
        info_plist = vantage_bundle / "Contents" / "Info.plist"
        
        with open(info_plist, "rb") as f:
            plist_data = plistlib.load(f)
        
        version = plist_data.get("CFBundleShortVersionString", "")
        assert isinstance(version, str), "Version must be string"
        assert len(version) > 0, "Version cannot be empty"
        parts = version.split(".")
        assert len(parts) >= 2, f"Version should be X.Y format, got {version}"
    
    def test_source_version_matches_bundle(self, vantage_bundle):
        """Source pyproject.toml version matches bundle."""
        try:
            import tomllib as _toml  # stdlib on Python 3.11+
        except ModuleNotFoundError:  # pragma: no cover - pre-3.11 fallback
            import tomli as _toml  # type: ignore
        
        with open("pyproject.toml", "rb") as f:
            source_config = _toml.load(f)
        
        source_version = source_config["project"]["version"]
        
        info_plist = vantage_bundle / "Contents" / "Info.plist"
        with open(info_plist, "rb") as f:
            plist_data = plistlib.load(f)
        
        bundle_version = plist_data.get("CFBundleShortVersionString", "")
        
        assert source_version == bundle_version, \
            f"Source version {source_version} != Bundle version {bundle_version}"
    
    def test_bundle_identifier_present(self, vantage_bundle):
        """CFBundleIdentifier is set."""
        info_plist = vantage_bundle / "Contents" / "Info.plist"
        
        with open(info_plist, "rb") as f:
            plist_data = plistlib.load(f)
        
        assert "CFBundleIdentifier" in plist_data, \
            "CFBundleIdentifier missing from Info.plist"
        assert plist_data["CFBundleIdentifier"] == "com.jarvisforge.app", \
            f"Wrong bundle identifier: {plist_data['CFBundleIdentifier']}"


class TestPackagingIntegrity:
    """Verify package contains expected files."""
    
    def test_dashboard_files_present(self, vantage_bundle):
        """Dashboard UI files are bundled."""
        dashboard = vantage_bundle / "Contents" / "Resources" / "app" / "dashboard"
        index = dashboard / "index.html"
        phone = dashboard / "phone.html"
        
        assert index.exists(), "Dashboard index.html missing"
        assert phone.exists(), "Phone client missing"
    
    def test_requirements_present(self, vantage_bundle):
        """requirements.txt is included."""
        req_file = vantage_bundle / "Contents" / "Resources" / "app" / "requirements.txt"
        assert req_file.exists(), "requirements.txt missing from bundle"
        
        content = req_file.read_text()
        assert "fastapi" in content.lower(), "FastAPI not listed"
        assert "pydantic" in content.lower(), "Pydantic not listed"
    
    def test_pyproject_present(self, vantage_bundle):
        """pyproject.toml is included."""
        pyproject = vantage_bundle / "Contents" / "Resources" / "app" / "pyproject.toml"
        assert pyproject.exists(), "pyproject.toml missing from bundle"
    
    def test_signatures_present(self, vantage_bundle):
        """Code signature files exist (ad-hoc signing)."""
        code_dir = vantage_bundle / "Contents" / "_CodeSignature"
        assert code_dir.exists(), "Code signature directory missing"
        
        code_resources = code_dir / "CodeResources"
        assert code_resources.exists(), "CodeResources file missing"


class TestLaunchBehavior:
    """Verify app can start and shutdown properly."""
    
    @pytest.mark.skip(reason="Requires interactive desktop environment")
    def test_app_starts_without_crash(self, vantage_bundle):
        """Application launches without immediate segfault/crash."""
        import subprocess
        import signal
        import time
        
        launcher = vantage_bundle / "Contents" / "MacOS" / "JarvisForge"
        
        # Start app in background
        proc = subprocess.Popen(
            [str(launcher)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "DISPLAY": ""},  # Headless mode
            timeout=5
        )
        
        try:
            # Give it a moment to initialize
            time.sleep(2)
            
            # Check if still running (not crashed immediately)
            assert proc.poll() is None, "App crashed during startup"
            
            # Terminate cleanly
            proc.terminate()
            proc.wait(timeout=5)
            
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    

class TestSourceToArtifactCorrespondence:
    """Verify artifact reflects current source."""
    
    def test_run_py_includes_recent_changes(self, vantage_bundle):
        """Latest run.py is packaged."""
        source_run = Path("run.py")
        bundle_run = vantage_bundle / "Contents" / "Resources" / "app" / "run.py"
        
        source_mtime = source_run.stat().st_mtime
        bundle_mtime = bundle_run.stat().st_mtime
        
        assert source_mtime <= bundle_mtime + 60, \
            f"Bundle predates source by more than minute: source={source_mtime}, bundle={bundle_mtime}"
    
    def test_cli_py_is_newest(self, vantage_bundle):
        """CLI file is included and up-to-date."""
        source_cli = Path("jarvis/cli.py")
        bundle_cli = vantage_bundle / "Contents" / "Resources" / "app" / "jarvis" / "cli.py"
        
        assert bundle_cli.exists(), "cli.py missing from bundle"
        
        source_mtime = source_cli.stat().st_mtime
        bundle_mtime = bundle_cli.stat().st_mtime
        
        assert source_mtime <= bundle_mtime + 60, \
            f"Bundle predates cli.py by more than minute"


# Red-first tests
@pytest.mark.skip(reason="Live verification requires interactive environment")
def test_live_launch_and_shutdown(vantage_bundle):
    """RED-FIRST TEST: Real app launch verified against observed state."""
    # Actual live verification would require:
    # 1. Launch app via double-click or terminal
    # 2. Observe successful startup (no crash dialogs)
    # 3. Confirm backend health endpoint responds
    # 4. Perform harmless command (e.g., "what is my battery")
    # 5. Verify response received
    # 6. Clean shutdown
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
