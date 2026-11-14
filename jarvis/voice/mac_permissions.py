"""macOS Permission Detection (Phase 80).

Detects real macOS permissions:
- Microphone permission
- Accessibility permission
- Screen recording permission
- Automation permission

Returns accurate GRANTED/DENIED/NOT_DETERMINED/UNAVAILABLE states.
Never fakes success.
"""

from __future__ import annotations

import subprocess
import plistlib
from typing import Any, Dict, List
from .base import PermissionState


class MacPermissionDetector:
    """Detect macOS accessibility permissions."""
    
    SYSTEM_PRIVACY_PLIST = "/Library/Application Support/com.apple.TCC/TCC.db"
    
    @staticmethod
    def get_service_access(service: str) -> bool:
        """Check if service has access using tccutil."""
        
        try:
            result = subprocess.run(
                ["tccutil", "services"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                # Parse output to find our service
                services = result.stdout.lower()
                return service.lower() in services
                
        except Exception:
            pass
        
        return False
    
    @classmethod
    def detect_microphone_permission(cls) -> PermissionState:
        """
        Detect macOS microphone permission.
        
        Returns:
            GRANTED - Application has microphone access
            DENIED - Application explicitly denied
            NOT_DETERMINED - User hasn't been prompted yet
            UNAVAILABLE - System doesn't support or TCC not available
            UNKNOWN - Error detecting permission
        """
        try:
            result = subprocess.run(
                ["tccutil", "status", "microphone"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return PermissionState.UNAVAILABLE
            
            output = result.stdout.lower()
            
            if "denied" in output or "not allowed" in output:
                return PermissionState.DENIED
            elif "granted" in output or "allowed" in output:
                return PermissionState.GRANTED
            elif "not found" in output or "none" in output:
                return PermissionState.NOT_DETERMINED
            else:
                return PermissionState.UNKNOWN
                
        except FileNotFoundError:
            # tccutil not available (e.g., non-macOS)
            return PermissionState.UNAVAILABLE
        except Exception:
            return PermissionState.UNKNOWN
    
    @classmethod
    def detect_accessibility_permission(cls) -> PermissionState:
        """
        Detect macOS Accessibility permission.
        
        Required for:
        - App control
        - Window manipulation
        - Screen reading
        """
        try:
            result = subprocess.run(
                ["tccutil", "status", "accessibility"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return PermissionState.UNAVAILABLE
            
            output = result.stdout.lower()
            
            if "denied" in output or "not allowed" in output:
                return PermissionState.DENIED
            elif "granted" in output or "allowed" in output:
                return PermissionState.GRANTED
            elif "not found" in output or "none" in output:
                return PermissionState.NOT_DETERMINED
            else:
                return PermissionState.UNKNOWN
                
        except FileNotFoundError:
            return PermissionState.UNAVAILABLE
        except Exception:
            return PermissionState.UNKNOWN
    
    @classmethod
    def detect_screen_recording_permission(cls) -> PermissionState:
        """
        Detect macOS screen recording permission.
        
        Required for:
        - Screenshot capture
        - Screen observation
        """
        try:
            result = subprocess.run(
                ["tccutil", "status", "screenrecord"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return PermissionState.UNAVAILABLE
            
            output = result.stdout.lower()
            
            if "denied" in output or "not allowed" in output:
                return PermissionState.DENIED
            elif "granted" in output or "allowed" in output:
                return PermissionState.GRANTED
            elif "not found" in output or "none" in output:
                return PermissionState.NOT_DETERMINED
            else:
                return PermissionState.UNKNOWN
                
        except FileNotFoundError:
            return PermissionState.UNAVAILABLE
        except Exception:
            return PermissionState.UNKNOWN
    
    @classmethod
    def detect_automation_permission(cls) -> PermissionState:
        """
        Detect macOS Automation permission.
        
        Required for scripting other apps via AppleScript.
        """
        try:
            result = subprocess.run(
                ["tccutil", "status", "appleevents"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return PermissionState.UNAVAILABLE
            
            output = result.stdout.lower()
            
            if "denied" in output or "not allowed" in output:
                return PermissionState.DENIED
            elif "granted" in output or "allowed" in output:
                return PermissionState.GRANTED
            elif "not found" in output or "none" in output:
                return PermissionState.NOT_DETERMINED
            else:
                return PermissionState.UNKNOWN
                
        except FileNotFoundError:
            return PermissionState.UNAVAILABLE
        except Exception:
            return PermissionState.UNKNOWN
    
    @classmethod
    def get_all_permissions_summary(cls) -> Dict[str, PermissionState]:
        """Get summary of all relevant permissions."""
        return {
            "microphone": cls.detect_microphone_permission(),
            "accessibility": cls.detect_accessibility_permission(),
            "screen_recording": cls.detect_screen_recording_permission(),
            "automation": cls.detect_automation_permission(),
        }
    
    @classmethod
    def requires_microphone(cls) -> bool:
        """Check if we actually have microphone permission."""
        return cls.detect_microphone_permission() == PermissionState.GRANTED
    
    @classmethod
    def requires_accessibility(cls) -> bool:
        """Check if we actually have Accessibility permission."""
        return cls.detect_accessibility_permission() == PermissionState.GRANTED
    
    @classmethod
    def get_required_permissions_for_voice(cls) -> List[str]:
        """Get list of permissions required for voice functionality."""
        required = []
        
        if cls.requires_microphone():
            required.append("microphone")
        
        return required
    
    @classmethod
    def get_required_permissions_for_mac_control(cls) -> List[str]:
        """Get list of permissions required for Mac control."""
        required = []
        
        if cls.requires_accessibility():
            required.append("accessibility")
        
        if cls.requires_automation():
            required.append("automation")
        
        return required


# Singleton instance for global access
DEFAULT_PERMISSIONS_DETECTOR = MacPermissionDetector()