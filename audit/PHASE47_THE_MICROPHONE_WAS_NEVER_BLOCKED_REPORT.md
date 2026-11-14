# Phase 47 — The microphone was never blocked by Apple

Closed 2026-10-04. This is the plan's **Phase 2 — Voice** (`JARVIS_Forge_Full_Working_Plan.md`
§1947: wake word, STT, TTS; test `"Jarvis, open VS Code."`), which had been reported
environment-blocked since Phase 43 and was not blocked at all.

---

## 1. Why this phase, and what was believed first

Carried as open since Phase 43: **43-A** the mic button waits on ears, **43-B** availability
is a boolean where three states are needed, **46-A** `can_listen` claims backends rather than
hearing, **45-A**/42-A voice from the phone is unbuilt. The stated cause everywhere was
"macOS refuses Speech Recognition to this process". That sentence appeared in the README's
hardware-truth table, in the marker file, in the error text, and in `ears_advice()`.

It had never been tested against the thing it blamed. The marker in the user's home was dated
**Sep 25** — before the desktop bundle existed (Phase 36 staged the source; Phase 37 renamed
the app). Every later claim inherited that diagnosis.

So the phase started with the one measurement that had not been made: send a real spoken clip
to a real Vantage and see what macOS actually does.

## 2. The measurement chain, in order

Fixture: `/tmp/p43.aiff` — 22 kHz mono AIFF, `say -o … "what is my battery"`, the same clip
Phase 43 used.

| #   | Setup                                                                                                | Result                                                                                                                                   |
| --- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Isolated instance, fresh home, `.tcc_blocked` absent                                                 | `/voice/status` → **`can_listen: true`** — before anything had ever been tried                                                           |
| 2   | POST the clip to it                                                                                  | `ok: false`, `error: macOS Speech blocked this process… Run JARVIS from a bundled host`, **and `can_listen: true` in the same response** |
| 3   | Read the crash report                                                                                | `namespace: TCC`, `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__`, "attempted to access privacy-sensitive data without a usage description"  |
| 4   | Build the helper inside its **own** signed `.app` with `NSSpeechRecognitionUsageDescription` present | still SIGABRT — so "be a bundled host" is not the variable                                                                               |
| 5   | Clear the stale marker, launch the **delivered** app with `open`, POST the clip                      | SIGABRT gone. New failure: `speech authorisation request timed out` after 20 s                                                           |
| 6   | User confirms a prompt appeared and clicks **Allow**; retry                                          | `speech authorisation request timed out` → then `transcription timed out` after 60 s                                                     |
| 7   | Read the helper                                                                                      | both waits block the **main thread** on a `DispatchSemaphore`, while `SFSpeechRecognitionTask` delivers on the **main queue**            |
| 8   | Fix the waits; build the helper for the machine's own architecture; retry                            | **`ok: true, text: "What is my battery", confidence: 0.94, duration_ms: 637`**                                                           |

### The two real causes, both ours

- **A self-deadlock.** `jarvis/voice/native/macos_speech.swift` waited on a semaphore on the
  main thread for callbacks that Apple delivers _on the main queue_. The answer could never
  arrive on the thread that was blocking it, so every call ran to its deadline — and a timeout
  is indistinguishable from a refusal in the error text. This is why the failure survived
  twelve phases: it looked exactly like the thing it was blamed on.
- **An inherited architecture.** The helper the app built was `x86_64` on an `arm64` machine
  (`file` on both, measured), taking **4m08s** to compile; the same source built natively takes
  **2.2 s**. The target was inherited from the parent, never chosen. `swiftc` is now pinned to
  `-target <platform.machine()>-apple-macosx11.0`.

Neither was an Apple refusal. What Apple actually required was the thing the user did in five
seconds: click Allow.

## 3. Availability is now a fact with three states

`43-B` prescribed this shape and it is implemented: `proven` | `refused` | `untested`, per
backend, persisted in the data home (`.proven_<backend>` written only by a transcription that
returned non-empty text; the existing `.tcc_blocked` stays the refusal record, so no migration).

| field         | meaning                             | fresh install | after this phase, on this machine |
| ------------- | ----------------------------------- | ------------- | --------------------------------- |
| `available()` | may be tried                        | true          | true                              |
| `state`       | what trying it last revealed        | `untested`    | `proven`                          |
| `can_listen`  | **has** transcribed here            | **false**     | true                              |
| `may_try`     | a client should still offer its mic | true          | true                              |

`can_listen` becoming "proven only" would have made the first transcription impossible — the
attempt is the only thing that produces the proof — so the phone client now gates on `may_try`
and says which state the laptop is in ("never tried on this laptop — tap and it will find out"
vs "the laptop has no working microphone" + the remedy). A button that can only be pressed
after it has already worked is not a button.

`/voice/transcribe` no longer answers `can_listen` from the backend's class identity; it reads
the state back after the attempt, which is what makes the measured
`{ok: false, error: "transcription timed out", can_listen: true}` impossible to produce.

The dashboard's Voice card stopped printing the first truthy `available` entry — `transcript`
is always truthy, so a machine with no microphone at all was reporting its own test stand-in
as the speech backend (43-C). It now reads `macos_speech · proven`.

## 4. The advice text was sending people to a step already done

"Run JARVIS from a bundled host" was written when the only observed case was a bare binary.
Measured at step 4 and 5 above: bundling is neither sufficient nor the variable — the variable
is whether macOS will attribute the process to an app with a usage string, which is decided by
**how it was launched** (`open Vantage.app` vs a terminal). Both the marker text and
`ears_advice()` now name that, and a test pins that the old unactionable phrasing is gone.

## 5. Tests

`tests/test_phase47_the_mic_was_never_broken.py` — **12**, written against the measured chain:

```
test_the_helper_waits_by_pumping_the_run_loop
test_both_speech_callbacks_return_to_the_main_queue
test_the_recognition_task_is_kept_alive
test_the_build_pins_the_native_architecture
test_availability_reports_three_states
test_a_successful_transcript_is_what_makes_a_backend_proven
test_a_refusal_is_remembered_as_refused_not_absent
test_the_refusal_advice_does_not_send_the_user_to_a_done_task
test_the_phone_offers_the_mic_on_an_untested_laptop
test_the_dashboard_says_which_state_the_ears_are_in
test_a_failed_transcription_never_answers_can_listen_true
test_voice_status_exposes_the_state_not_only_a_boolean
```

Honest note on order: the two Swift fixes were made and proven live **before** this file
existed, because the measurement _was_ the experiment — steps 7 and 8 are the fix and its
proof, not a red/green cycle. Everything in §3 and §4 was written red-first (6 failed, 4
passed on the first run, then 3 failures from my own test bugs: patching read-only
properties, a fake process with no `communicate()`, and a missing import).

Two of those bugs are worth naming because they are the recurring kind. The `native` fixture
initially left a `.tcc_blocked` marker in the session's home, and `tests/test_voice.py` then
asserted the toolchain was present — a leak, not a failure, and the symptom this project has
already recorded twice. And the phone page shipped a duplicate `let earReason`, caught by the
`node --check` guard that Phase 46 added for exactly this.

## 6. Live, on the delivered app

Rebuilt, re-signed (`valid on disk`, `satisfies its Designated Requirement`), copied to
`~/Downloads/Vantage`, launched with `open`:

```
POST /voice/transcribe  (clip: "what is my battery")
  → {"ok":true,"backend":"macos_speech","text":"What is my battery",
     "confidence":0.94,"duration_ms":17476,"state":"proven","can_listen":true}
     (17.5 s = the arm64 rebuild of the helper, which the app did itself; 637 ms afterwards)
  → helper on disk: Mach-O 64-bit executable arm64

GET /voice/status
  → state: proven | can_listen: true | may_try: true
  → states: {macos_speech: proven, whisper_cpp: untested, openai_whisper: untested}

POST /voice/command {"text":"vantage what is my battery","speak":true}
  → transcript in : 'vantage what is my battery'      (wake word stripped)
    intent        : laptop_battery
    reply         : Your laptop battery is at 100% (fully charged).
    spoken        : true · decision: EXECUTE · needs_confirmation: False

Phone client at 375 px: mic button ENABLED — "tap to speak, tap again to stop", no disclosure
  needed, because the state is proven. 0 console errors.
```

That is the plan's Phase 2 loop: **audio → transcript → wake word → intent → permission →
tool → verification → reply → spoken.**

## 7. Numbers, re-measured

|                                       | before                               | after                                           | how                                                      |
| ------------------------------------- | ------------------------------------ | ----------------------------------------------- | -------------------------------------------------------- |
| Test suite                            | 1884 passed · 0 failed               | **1896 passed · 0 failed · 0 skipped** (70.9 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 47 tests                        | 0                                    | **12**                                          | one new module                                           |
| Test modules                          | 73                                   | **74**                                          | `ls tests/test_*.py                                      | wc -l` |
| Phase reports                         | 48                                   | **49**                                          | `ls audit/*.md                                           | wc -l` |
| Voice input on this machine           | "environment-blocked" since Phase 43 | **working, on-device, 637 ms, 0.94 confidence** | live, above                                              |
| Helper build time                     | 4m08s (x86_64, inherited)            | **~2 s (arm64, pinned)**                        | measured both ways                                       |
| First-call cost after a fresh install | unbounded (blocked the request)      | 17.5 s including the rebuild                    | live, above                                              |

## 8. Open items

- **47-A — the app exited cleanly during the first experiment and the cause is not
  established.** `~/.jarvis-forge/app.log` ends with uvicorn's `Shutting down` / `Waiting for
connections to close`, which is the SIGTERM path — a deliberate stop, not a crash, and no
  crash report exists for either process. The user was asked and it may simply have been them
  quitting the panel; it happened while a 90-second request was in flight and while `swiftc`
  children were running, so an unattributed coincidence is the honest description. If it
  recurs, the place to look is the launcher's quit path against a long-running child.
- **47-B — `proven` is per-home, and it is a cache, not a promise.** Delete the data directory
  and the machine is `untested` again; grant the permission and then revoke it in System
  Settings and the marker still says `proven` until an attempt fails. A state that is only
  written by success can go stale in exactly that direction.
- **47-C — the first attempt still costs a helper build.** 17.5 s on a cold home, inside the
  request. The arch pin made it seconds rather than minutes; shipping the compiled helper in
  the bundle (or building it at first launch instead of first utterance) would remove it.
- **47-D — a spoken command cannot yet reach a _risky_ action from the phone in one gesture.**
  The mic path is transcribe → `/command`, and an action needing confirmation raises a card the
  phone then approves — two round trips, which is correct for safety and slower than speaking.
- **47-E — `whisper_cpp` and `openai_whisper` remain `untested`**, so the three-state machine is
  proven end to end on one backend only; the other two share the code but have never earned
  their own `proven` marker here.
- **47-F — the plan's literal acceptance sentence was not used.** The loop was proven with
  "what is my battery" because it is read-only. `"Vantage, open VS Code."` is reachable by the
  same path and was deliberately not fired without asking, since it opens an editor on the
  user's screen.

## 9. What this changes for the reader

Voice input works on this laptop, on-device, with no key and no network: the phone's mic
button is live, and the fix was not a permission workaround but a run loop and an
architecture flag. The twelve-phase sentence "macOS denied the microphone" was a timeout
wearing a refusal's clothes.
