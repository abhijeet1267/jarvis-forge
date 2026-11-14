# Phase 43 — a passthrough is not a microphone

## 1. Why this phase exists before the mic button

The ask is voice from the phone. Before building a button on top of the speech path, one real
recording was made (`say -o /tmp/p43.aiff "what is my battery"`) and sent to the running app:

```
POST /voice/transcribe   file=p43.aiff
  -> {"ok": true, "backend": "transcript", "text": "",
      "confidence": 1.0, "error": "", "duration_ms": 0}
```

**Nothing on this machine can transcribe.** Measured:

| Backend          | State                                                                                                                                                                                        |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `macos_speech`   | Apple refuses it: _"macOS Speech blocked this process. Apple only grants Speech Recognition to a signed application bundle…"_ A `.tcc_blocked` marker has sat in the data home since Sep 25. |
| `whisper_cpp`    | not installed (`brew install whisper-cpp` would)                                                                                                                                             |
| `openai_whisper` | no key                                                                                                                                                                                       |
| `transcript`     | available — and it is **not a microphone**: it echoes a string a test handed it                                                                                                              |

So `get_stt()` fell through to `TranscriptSTT`, which returned `ok=True, confidence=1.0` for a
file it never listened to. A mic button built on that endpoint would have shown _"heard
nothing"_ as a success, and anything downstream that trusts `ok` would have acted on silence.
This is the same shape the project has caught four other times — a stand-in answering a
production request (Phase 18's simulated actuation, Phase 39's stubbed `run_command`, Phase
33's cross-attributed identifier, Phase 15's false successes) — so the fix went at the source
of the claim rather than at the caller.

## 2. The fix

- `STTBackend.real` says which kind of thing a backend is. `TranscriptSTT.real = False`.
- `TranscriptSTT.transcribe()` **refuses an audio file it was not fed a transcript for**, and a
  fed transcript now carries `meta.passthrough = True` so a test's answer is labelled as one.
  The harness that 20+ voice tests rely on still works — proven by
  `test_a_fed_transcript_is_still_the_test_harness_it_was`.
- `/voice/transcribe` never returns `ok` from a non-real backend. It returns an honest failure
  naming what would give the machine ears, cheapest first: grant Speech Recognition to Vantage
  and clear the marker; or install whisper.cpp for offline; or set a hosted key, **which sends
  the audio off this machine** — said in the sentence itself, not discovered later.
- `/voice/status` gains `can_listen`, which is `false` when no real backend works. Before this,
  `available: {transcript: true}` could be read by a client as "there is a way to hear", because
  a passthrough was counted in the same column as a microphone.

## 3. Live, on the delivered app

Rebuilt, re-signed, copied to `~/Downloads/Vantage`, launched, and the **same clip** sent to the
same endpoint:

```
ok: False | backend: transcript | confidence: 0.0 | can_listen: False
error: "No microphone backend is working here. Three ways to fix it, cheapest first:
        (1) System Settings → Privacy & Security → Speech Recognition, allow Vantage, then
        delete ~/.jarvis-forge/voice/native/.tcc_blocked so the app retries Apple's on-device
        transcription; (2) install whisper.cpp …; (3) set a hosted key … which sends the audio
        off this machine."

GET /voice/status -> stt.can_listen: false
```

A machine that cannot hear now says so, and says what would change that.

## 4. What still has to happen before voice works

The mic button is deliberately **not** built yet: a button that always fails is a worse
interface than no button, and the honest answer is on the wire now. Three paths, in cost order:

1. **Free, local, private.** System Settings → Privacy & Security → **Speech Recognition** →
   allow Vantage (the bundle already declares `NSSpeechRecognitionUsageDescription`: _"Vantage
   transcribes your speech on-device to run commands"_), then delete
   `~/.jarvis-forge/voice/native/.tcc_blocked`. The permission is the user's to grant — I cannot
   and should not.
2. **Free after one install.** `brew install whisper-cpp` — offline, no permission dance.
3. **Cloud.** A key (Phase 42's `JARVIS_LLM_API_KEY`), which ships audio to whoever the endpoint
   names.

Any of those makes `can_listen` true, and then the phone gets a microphone button that means
something.

## 5. Numbers

```
new tests        6
full suite       1833 passed · 0 failed · 0 xfailed · 0 skipped
tools            133 · planner routes 45 · API routes 108/106      (all unchanged)
test modules     70          reports 45
size             62,261 lines of versioned Python/HTML/JS (39,176 source · 23,085 tests)
```

## 6. Carried forward

- **43-A** the microphone button itself, which now has somewhere honest to send audio the
  moment `can_listen` is true.
- **43-B** `MacOSSpeechSTT.available()` means _"swiftc exists and Apple has not refused us in
  this home yet"_, while the abstract contract says _"can transcribe on this machine"_. On a
  fresh home it is optimistic and the first real clip discovers the truth. The right shape is a
  three-state answer — untested / refused / proven — with a successful transcript as the proof;
  probing costs a SIGABRT, so it cannot be done lazily at boot.
- **43-C** `/voice/status`'s `available.transcript` is still `true`, which is accurate about the
  object and misleading in a list of microphones. Only `can_listen` is now load-bearing.
- **43-D** the clip used here was a dummy payload once the app was rebuilt; Apple's own refusal
  arrived before it was decoded, so no test depends on the file being real audio. A recording
  with actual speech is the stronger fixture.
- Unchanged: 42-B…D, 41-B…F, 40-A…E, 39-A…E, 38-A…D, 37-A…E, 36-A…E, 35-A…C, 34-A…C.

## 7. Git

Committed on `main`, not pushed. Files: `jarvis/voice/stt.py`, `jarvis/api/app.py`,
`tests/test_phase43_a_passthrough_is_not_a_mic.py`, `README.md`, `benchmarks/*`, `assets/*`,
this report.
