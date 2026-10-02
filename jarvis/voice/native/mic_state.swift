// mic_state.swift
//
// Reports the microphone truth for Vantage: whether macOS has granted
// this process access, and which input devices actually exist.
//
// Compiled on demand by jarvis/voice/audio.py, like the OCR helper.
//
// Usage:  mic_state
// Output: {"permission": "authorized|denied|notDetermined|restricted|unknown",
//          "devices": [{"name": "...", "in_use": false}]}
//
// This helper deliberately *queries* consent instead of requesting it.
// AVCaptureDevice.requestAccess(from:) presents a prompt tied to the calling
// bundle; a bare command-line binary has none, so requesting access either
// never answers or aborts the process. Querying is instant and side-effect
// free, which is what makes it safe to call from a health poll.

import Foundation
import AVFoundation

func permissionName(_ status: AVAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorized: return "authorized"
    @unknown default: return "unknown"
    }
}

var devices: [[String: Any]] = []
let session = AVCaptureDevice.DiscoverySession(
    deviceTypes: [.microphone],
    mediaType: .audio,
    position: .unspecified)
for device in session.devices {
    devices.append([
        "name": device.localizedName,
        "in_use": device.isInUseByAnotherApplication,
    ])
}

let payload: [String: Any] = [
    "permission": permissionName(AVCaptureDevice.authorizationStatus(for: .audio)),
    "devices": devices,
]

guard let data = try? JSONSerialization.data(withJSONObject: payload),
      let text = String(data: data, encoding: .utf8) else {
    print("{\"permission\":\"unknown\",\"devices\":[]}")
    exit(1)
}
print(text)
