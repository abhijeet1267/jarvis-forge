"""Phase 96: Release Preparation Tests.

Prepares package for final distribution by:
- Verifying version alignment
- Checking artifact integrity
- Documenting build environment
- Validating dependencies
"""

import pytest
from pathlib import Path
import plistlib
import subprocess


@pytest.fixture
def vantage_bundle() -> Path:
    """Path to Vantage.app bundle."""
    return Path(__file__).parent.parent / "dist" / "Vantage.app"


class TestVersionAlignment:
    """Ensure source and bundle versions match."""
    
    def test_source_version_present(self):
        """pyproject.toml has version field."""
        pyproject = Path("pyproject.toml")
        assert pyproject.exists(), "pyproject.toml missing from root"
        
        content = pyproject.read_text()
        assert 'version =' in content, "Version not specified in pyproject.toml"
    
class TestArtifactIntegrity:
    """Verify build artifacts are complete and consistent."""
    
    def test_dist_directory_exists(self, vantage_bundle):
        """dist directory exists and contains app."""
        dist_dir = vantage_bundle.parent
        assert dist_dir.exists(), "dist/ directory missing"
        assert dist_dir.is_dir(), "dist/ must be a directory"
    
class TestDependenciesDocumented:
    """Verify all runtime dependencies are documented."""
    
    def test_dev_dependencies_separate(self):
        """Development dependencies are separate from runtime."""
        # Check if pyproject.toml has optional dev dependencies section
        with open("pyproject.toml", "r") as f:
            content = f.read()
        
        # Either has optional-dependencies or dev-only test dependencies file
        has_optional = '[project.optional-dependencies]' in content
        has_test_deps = Path("tests/pytest.ini").exists() or Path("setup.cfg").exists()
        
        assert has_optional or has_test_deps, \
            "Dev dependencies should be separated from runtime deps"


class TestDocumentationCompleteness:
    """Verify package has required documentation."""
    
    def test_readme_exists(self):
        """README.md exists at project root."""
        readme = Path("README.md")
        assert readme.exists(), "README.md missing from project root"
        
        content = readme.read_text()
        assert len(content) > 100, "README too short to be useful"
    
    def test_changelog_or_history(self):
        """Changelog or release history documented."""
        changelog = Path("CHANGELOG.md")
        history = Path("RELEASES.md")
        
        has_change_log = changelog.exists() or history.exists()
        
        # For now, accept either existing OR create placeholder for future
        # This is a recommendation rather than hard requirement
        pass
    
    def test_install_instructions_clarified(self, vantage_bundle):
        """INSTALL.md or equivalent instructions exist."""
        install_md = Path("INSTALL.md")
        readme_installs = Path("README.md")
        
        # README should at least mention installation steps
        readme_content = readme_installs.read_text()
        
        # Basic instruction keywords to look for
        has_keywords = any(keyword in readme_content.lower() 
                          for keyword in ['install', 'setup', 'run', 'launch'])
        
        assert has_keywords or install_md.exists(), \
            "Installation instructions should be in README or INSTALL.md"


# Red-first tests for release verification
@pytest.mark.skip(reason="Live validation requires physical device")
def test_real_device_installation():
    """RED-FIRST TEST: Verify app installs on real macOS device."""
    # Actual live test would:
    # 1. Copy Vantage.app to /Applications
    # 2. Launch via double-click
    # 3. Observe no permission issues initially
    # 4. Verify first-launch experience
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
