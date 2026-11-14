# VANTAGE — Testing Guide

Commands, tiers, prerequisites and regression rules. Every number here was measured on
this tree (2026-10-21).

---

## 1. Prerequisites

| Need                  | Requirement                                    | If missing                                                                                       |
| --------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Python suite          | `.venv` in the repo (Python 3.11)              | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`                             |
| Optional (vision/PDF) | `pillow`, `pypdf` (both in `requirements.txt`) | the attachment pipeline reports `not_processed`/`unavailable` — truthfully, never a fake success |
| macOS app             | Xcode or CommandLineTools (Swift 5.9+)         | `swift build` in `macos/Vantage` fails with no compiler                                          |
| Android               | JDK 17–21 + Android SDK                        | AGP 8.7 rejects JDK 22+; point `ANDROID_HOME` at the SDK                                         |
| iOS                   | Xcode                                          | **project is paused** — do not build by default                                                  |

---

## 2. The commands

```bash
# full Python suite (the gate before a release or a milestone)
.venv/bin/python -m pytest -q -p no:cacheprovider
#   expected: 2799 passed, 38 skipped, 0 failed   (~170 s)

# one file / one test while iterating
.venv/bin/python -m pytest tests/test_api_contract.py -q
.venv/bin/python -m pytest tests/test_phase74_attachments.py -k pdf -q

# with the reason for every skip printed (audit what is actually being skipped)
.venv/bin/python -m pytest -q -rs

# Android unit tests (needs JDK 17 + ANDROID_HOME)
cd android && ./gradlew testDebugUnitTest --rerun-tasks    # 46 tests
cd android && ./gradlew :app:assembleDebug

# native macOS app: build the release bundle + verify its signature
cd macos/Vantage && make app
codesign --verify --strict macos/Vantage/Vantage.app

# run the backend the way a person does
.venv/bin/python run.py serve        # :8765
```

---

## 3. Test tiers

| Tier                         | What it covers                                                                | Where                                                                                                                           | Speed       |
| ---------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| **1 — unit**                 | one function/module: planner rules, verification semantics, schema validation | `tests/test_planner.py`, `tests/test_api_contract.py`, `tests/test_paths.py`                                                    | < 1 s       |
| **2 — contract**             | the published API shape, provider adapters, service boundaries                | `tests/test_api_contract.py`, `tests/test_phase64_the_api_shape_is_not_for_passersby.py`, `tests/test_ai_chat_provider_path.py` | ~1 s        |
| **3 — integration**          | database + lifecycle + auth + multi-module                                    | `tests/test_api.py`, `tests/test_phase74_attachments.py`, `tests/test_pipeline.py`                                              | ~5 s        |
| **4 — end-to-end (backend)** | a full user sentence through planner → policy → tool → verification           | `tests/test_phase12_command_path.py`, `tests/test_phase74_attachments.py`                                                       | ~5 s        |
| **5 — platform acceptance**  | the packaged macOS app, the companions                                        | `macos/Vantage/Tests/`, `android/app/src/test/`                                                                                 | build-gated |

**Mocks are not acceptance evidence.** A mocked provider proves the _branch_, never that a
real model answers, a real app renders, or a real device pairs. Those are called out
explicitly wherever they are BLOCKED in `PROJECT_COMPLETION_MATRIX.md`.

---

## 4. Prerequisites the suite encodes

- `tests/conftest.py` redirects `JARVIS_HOME` to a temp dir, so no test touches the real
  `~/.jarvis-forge` (memory, audit chain, token, keychain).
- The keychain is swapped for an in-memory backend, so credential tests never write to the
  real macOS keychain.
- An autouse fixture resets the AI-provider selection, the embedding cache and the route
  health ledger between tests — those are module-level singletons and would otherwise leak
  between test files (this was a real order-dependence bug, fixed in iter 1).

---

## 5. Regression rules

1. **A defect gets a test that fails under the old behavior.** Not a test that passes
   both before and after — that proves nothing.
2. **Never weaken an assertion to make a suite green.** If a contract changes, change the
   contract, its test and its consumers _in the same cycle_.
3. **Never delete a test to get a green run.** A test may be retired only when the
   behaviour it pinned is genuinely gone, and the equivalent coverage must exist
   elsewhere — and the retirement is recorded with the reason.
4. **Do not add blanket skips.** A skip needs a named external prerequisite (no device,
   no permission, no credential), and the count is reported with the run.
5. **Order-independence is a property of the suite, not of one file.** Run the suite
   twice with the new test file in different positions when touching shared state.
6. **Run the full suite after a change to a shared module** (models, config, security,
   the API layer) — not just the tests of the file you edited.
7. **A platform build is part of the gate** when the change touches that platform.

---

## 6. What the suite does not cover (be honest about it)

- **Live provider inference** — mocked; the real path needs a credential and is marked
  BLOCKED in the completion matrix.
- **The packaged macOS app's UI** — Swift tests cover `BackendManager` state logic; the
  rendered app needs a human (or an approved permission) to accept the local-network
  prompt.
- **Android on a device** — unit tests and a build pass; install/launch needs a device.
- **iOS** — paused by project instruction.
