# Vantage AI — native iOS companion module

An in-app Swift module that gives the Vantage client three things, with **no**
separate app, microservice, or third-party bot:

1. **Siri / Shortcuts integration** via Apple's `AppIntents` framework
   (`VantageSiriIntent.swift`) — "Hey Siri, ask Vantage a question".
2. **Any OpenAI-compatible AI client** (`AIClient.swift`) — OpenAI, OpenRouter,
   Groq, DeepSeek, Ollama, LM Studio, or any endpoint speaking the OpenAI spec.
3. **A SwiftUI settings screen** (`VantageAISettingsView.swift`) to point at a
   provider, store the key securely, fetch models, and pick one.

## Files

| File                                  | Responsibility                                                                                                                   |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `Sources/AISettingsManager.swift`     | Persistent config: API key in the **iOS Keychain**; `baseURL`, `selectedModel`, `systemPrompt`, `temperature` in `UserDefaults`. |
| `Sources/AIClient.swift`              | `fetchModels()` → `GET {baseURL}/models`; `sendMessage()` → `POST {baseURL}/chat/completions`. Typed errors; nothing fabricated. |
| `Sources/VantageSiriIntent.swift`     | `AskVantageAIIntent` (`AppIntent`) + `AppShortcutsProvider` phrases + a result snippet view.                                     |
| `Sources/VantageAISettingsView.swift` | SwiftUI form: base URL + presets, secure key field, **Fetch Models**, model picker, Siri guidance.                               |
| `Package.swift`                       | Makes the four files a buildable Swift module.                                                                                   |

## Security posture

- The API key is stored **only** in the Keychain (`kSecClassGenericPassword`,
  `kSecAttrAccessibleAfterFirstUnlock`). It is never written to `UserDefaults`,
  plists, or logs, and the settings screen uses an unbuffered `SecureField`
  (opt-in reveal shows only the _unsaved_ draft).
- Store/replace/delete report success only when `SecItem*` actually returned
  `errSecSuccess`; there is no plaintext fallback.
- Failed AI calls return a typed error and a dialog that says what failed — a
  request that did not return a usable answer is never shown as an answer.

## Verification (as run on the build host)

| Check               | Command                                                             | Result                                                                                  |
| ------------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Module compiles     | `swift build --package-path ios/VantageAI`                          | **Build complete** (exit 0)                                                             |
| Sources type-check  | `swiftc -typecheck -sdk "$(xcrun --show-sdk-path)" Sources/*.swift` | exit 0                                                                                  |
| Keychain round-trip | store → read → replace → read → delete → delete-again               | write/read/replace/delete all correct                                                   |
| `/models` parse     | local OpenAI-shaped mock                                            | `gpt-4o,gpt-4o-mini,test-model`                                                         |
| `/chat/completions` | local mock, good + bad model                                        | `pong from gpt-4o-mini`; bad model → `ERR 400: {"error":{"message":"model not found"}}` |

## Status: `BLOCKED_EXTERNAL_DEPENDENCY` (not built/running as an iOS app)

**This machine has no iOS toolchain and no existing Xcode/iOS project.** Measured:

- No `.xcodeproj`, `.xcworkspace`, `*.swift`, or `Info.plist` existed before this
  module was added — the repository is a Python/FastAPI + web-client project, and the
  desktop client is a Python-packaged `Vantage.app`. The phone client today is the
  **web page** `dashboard/phone.html` served by the laptop.
- `xcodebuild` is unavailable (Command Line Tools only), and the **iOS SDK cannot be
  located**: `xcrun --sdk iphoneos --show-sdk-path` → _SDK "iphoneos" cannot be
  located_. So an actual iOS `.app`/`.ipa` **cannot be built or run here.**
- The repository itself already records native mobile as `BLOCKED_EXTERNAL_DEPENDENCY`
  (README, Phase 58: "no native build, no signature, no handset -- measured").

Consequently the code is **written and compile-verified** (as a Swift module and by
type-check against the macOS SDK, where SwiftUI/AppIntents/Security all exist), and
its runtime logic is **exercised against a real local Keychain and a local mock
server** — but **running it inside a real Siri invocation / on-device is NOT VERIFIED**,
because that needs Xcode + the iOS SDK + a code-signing identity + a device or
simulator, none of which exist on this machine.

### To finish on a machine with Xcode

1. Create an iOS App target (bundle id `com.jarvisforge.vantage` to match the desktop
   boundary), iOS 16+ minimum.
2. Add these four files (or add `ios/VantageAI` as a local Swift package).
3. Nothing else is required to compile; the Siri phrases register automatically via
   `VantageAppShortcutsProvider`.
4. Run on a simulator/device to observe Siri dialogs and the settings screen for real.
