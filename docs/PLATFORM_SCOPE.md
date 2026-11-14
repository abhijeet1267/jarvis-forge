# Vantage / Jarvis Forge — Platform Scope

**Effective**: 2026-10-15

## Active Development Targets

### P0 — macOS (Primary)

- Native SwiftUI application at `macos/Vantage/`
- Compiled with `swift build -c release`
- Packaged with `make app` → `Vantage.app`
- Backend lifecycle managed by `BackendManager` (spawns `uvicorn` on `127.0.0.1:8765`)
- API client targets localhost with token auth from `~/.jarvis-forge/api_token`

### P1 — Android (Secondary Companion)

- Native Kotlin + Jetpack Compose application
- Communicates with the same Vantage backend over local network
- Uses device pairing + session tokens (not API token passthrough)
- Target: `android/` directory (to be created)

### P2 — iOS (Paused)

- Existing code preserved at `ios/VantageAI/`
- Contains VantageAI library (AI provider client, settings, Siri) and VantageCompanionCore (session management, API client, SwiftUI views)
- NOT actively developed — compilation errors from recent companion work remain unresolved
- No iOS releases planned

### P3 — Dashboard (Maintenance Only)

- HTML dashboard at `dashboard/index.html` remains as development/debug interface
- Not the primary user experience

## Platform Boundaries

| Capability                | macOS     | Android     | iOS       |
| ------------------------- | --------- | ----------- | --------- |
| Backend lifecycle control | Yes (P0)  | No          | No        |
| AI provider management    | Yes       | View only   | View only |
| Command execution         | Yes       | Yes         | Paused    |
| Approvals                 | Yes       | Yes         | Paused    |
| Device management         | Yes       | Yes         | Paused    |
| Voice control             | Yes       | Planned     | Paused    |
| Memory/Projects           | Yes       | Planned     | Paused    |
| Emergency stop            | Yes       | Yes         | Paused    |
| Pairing (new device)      | Host only | Client only | Paused    |
