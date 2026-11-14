# VANTAGE AUTONOMOUS COMPLETION LOOP

## 1. Mission and authority

Finish the existing Vantage project in this repository:
`/Users/abhijeetmiskin/AppData/vantage/jarvis-forge`

The goal is a working, usable, securely implemented, tested Vantage product — not a
repository that merely passes backend unit tests.

Work on Vantage only. Preserve the existing architecture and improve it incrementally
unless evidence proves a different design is necessary.

The current user instruction authorizes continuing implementation across the Vantage
backend, native macOS application, dashboard, AI Command Center, attachment pipeline,
integrations, and companion apps. Do not treat an old temporary pause or a prior agent's
completion report as permanent authorization to abandon a workstream. Check current
project instructions, then continue all work authorized by this mission.

Do not stop because one platform, API credential, integration, or test environment is
unavailable. Complete the remaining code, integration boundaries, mocks, regression tests,
documentation, and other feasible tasks while recording any genuinely external dependency.

## 2. Mandatory rules

1. Execute work, do not just report work.
2. A plan, audit, TODO list, green test suite, valid code signature, or updated tracking
   document does not prove that the product works.
3. After identifying a defect, fix it when safe, test it, inspect the diff, and move to the
   next unfinished acceptance criterion.
4. Never repeat the same investigation or recovery report unless genuinely new evidence
   changes the diagnosis.
5. Never declare a task complete solely because its code exists. Require observable
   evidence appropriate to the task.
6. Never invent test results, runtime observations, API credentials, provider access,
   device tests, successful actions, or completion percentages.
7. Preserve every pre-existing user change. Inspect status and diffs before editing files.
8. Never run destructive cleanup commands such as `git reset --hard`, `git clean -fd`,
   `git checkout -- <file>`, or bulk restore commands. Do not delete recovery files or
   untracked user work.
9. Do not change a test expectation merely to make a failure disappear. Determine the
   intended contract using implementation, documented behavior, history, and related tests.
10. Do not delete tests unless they are demonstrably dead or obsolete, and only after
    preserving the valid acceptance coverage elsewhere.
11. Do not add fake success results, hollow placeholder implementations, blanket skips, or
    weakened assertions.
12. Do not expose, print, commit, or place credentials in logs, reports, screenshots,
    source control, or test artifacts.
13. Do not send emails, create external calendar events, make purchases, publish releases,
    delete user information, or perform other consequential external actions without the
    appropriate authorization and confirmation.
14. Do not commit, push, publish, or force-push. Leave final Git changes reviewable unless
    the user explicitly directs otherwise.
15. Use parallel agents only for genuinely independent work. Prevent multiple agents from
    modifying the same files simultaneously. Inspect and test merged changes.
16. Keep a durable task tracker with status, acceptance criteria, changed files, test
    commands, real outcomes, and unresolved dependencies.
17. Keep meaningful progress updates concise. Report code changes and verification
    evidence, not continuous narration.

## 3. Anti-stall rule

Each invocation of this loop MUST result in an actual unit of progress, unless a required
command or service is genuinely unavailable.

Valid progress includes:

- Implementing a real feature or defect fix.
- Adding a meaningful regression test.
- Running an uncompleted verification step and inspecting its actual results.
- Building and launching the real application.
- Exercising an unverified acceptance workflow.
- Fixing a newly discovered runtime, integration, packaging, or security failure.
- Completing documentation necessary for a verified build, installation, or operating
  workflow.

Not valid as the only work in an iteration:

- Re-reading the previous completion report.
- Repeating an audit without a new hypothesis.
- Updating the completion matrix without changing or verifying anything.
- Declaring that no unblocked work exists without inspecting the actual acceptance criteria.
- Re-running an already-passing test suite without a code change or a specific reason.
- Producing another plan and waiting for the user.

If you have produced two consecutive progress reports without a real code change or new
verification result, stop reporting and select the next achievable task immediately.
If a task is blocked, record the exact error and dependency, then work on another track. Do
not retry the same blocked operation endlessly.

## 4. First establish current reality

Inspect:

- Current working directory, branch, `git status`, changed files, untracked files, recent
  history, and diffs.
- `CLAUDE.md`, project documentation, issue/task trackers, build instructions, and
  acceptance matrices.
- Existing tests, scripts, CI configuration, hooks, loop files, scheduled tasks, and active
  development processes.
- Backend status, health and authentication behavior.
- Current native macOS app source, app bundle, build output, signature, process, and logs.
- Dashboard provider configuration.
- AI Command Center request and response path.
- Attachment-processing implementations and remaining TODOs.
- iOS and Android project structure and available toolchains.
- Calendar, email, browser and other existing integrations.

First reconcile the old engineering log with current reality. The old log is not the source
of truth for the current application.

Create or update one canonical tracker in the repository's existing documentation style.
Suggested location if no equivalent exists: `docs/VANTAGE_COMPLETION_MATRIX.md`.

For each acceptance criterion, record:

- Status: NOT STARTED, IN PROGRESS, VERIFIED, or BLOCKED.
- Concrete evidence and command output.
- Missing verification, if any.
- Next action.

Do not use the tracker as a substitute for execution.

## 5. Priority P0 — preserve data, security and backend correctness

Verify and complete: startup, shutdown, health endpoint, authentication, authorization,
process cleanup, API contracts, token handling under `~/.jarvis-forge`, honest reporting of
tool execution/verification, error handling, validation, timeouts, cancellation, malformed
input, correct handling of unverified writes and consequential actions, and regression
coverage for observed defects.

Preserve backend port `8765` unless a documented requirement establishes otherwise. Do not
change it merely to simplify testing.

Run focused backend tests after changes; run the complete suite at milestone boundaries and
report exact counts and exit codes.

Treat `UNKNOWN` verification as distinct from verified success. A successful API response
must not be confused with independent verification if the tool's contract requires a
read-back or other evidence.

Preserve the invariant that a narrow phone surface cannot self-approve its own consequential
request, while local voice/dashboard behavior follows the documented authority model. Verify
both positive and negative cases.

## 6. Priority P1 — conversational AI that actually works

Inspect the complete request path: `jarvis/core/planner.py`, orchestration, provider
adapters, state management, native UI.

Implement and verify: configured valid OpenAI/Gemini model is the primary path for general
questions; rule-based detection remains for recognized deterministic actions and safety
boundaries; greetings/unrecognized-but-valid reasons/small talk are not misrouted to a
refusal or API-key warning when a model is configured; no duplicate/contradictory answers
from combining an unrelated rule reply with a model response; conversation history and
multi-turn follow-ups; distinguish conversational text, proposed actions, confirmations,
executions, verified outcomes; graceful handling of missing/invalid credentials, timeouts,
outages, empty/malformed responses, rate limits, cancellation, oversized input; approval
gates for external side effects; no leaking private content.

Add regression tests for all cases, including one proving the answer is from the configured
provider path rather than a hardcoded fallback.

Re-check the greeting fix in current source and request path — a passing isolated planner
test is not sufficient.

When real credentials are available through existing secure config, run a real end-to-end
inference test without exposing them. Otherwise complete and verify with mocks and mark
live-provider verification as a credential dependency.

## 7. Priority P1 — API provider configuration in the existing dashboard

Complete provider management in `dashboard/index.html` and the existing backend
key-management API: clear AI Providers UI; provider selection; secure key submission; use
the established secure store; never reveal full keys after saving; accurate
configured/unconfigured/saved/loading/error states; safe validation; useful failure
messages; end-to-end test through the actual dashboard and backend routes. Do not create a
separate provider page unless the architecture requires one.

## 8. Priority P1 — native macOS application must visibly work

Supported app: native SwiftUI under `macos/Vantage/`, bundle `com.jarvisforge.vantage`.
Target path historically `/Users/abhijeetmiskin/AppData/vantage/jarvis-forge/macos/Vantage/Vantage.app`.

Verify the current supported target and build process before using them. Do not revive a
retired Tk launcher.

Complete and verify: bundle built from latest source; correct bundle launches and
authenticates; health polling updates state/handles timeouts/cancels cleanly; no duplicate
polling, leaked tasks, stale health, orphaned processes; healthy → unavailable → recovered
transitions; fetch errors show useful messages + working Retry; Export/Revoke failures shown
truthfully; selection/loading/empty/error states; Command Center shows the real
conversational model response; network failure without freezing/stale-as-current.

For each runtime-relevant change: build the app, confirm the bundle holds the current
executable, check identifier + signature, launch the real `.app`, inspect logs/process/
health/API/UI where tools permit, exercise the acceptance path, repair failures.

The old log said the running process was older than the bundle — resolve with real process
inspection: launch a fresh instance and verify which executable is running. If safe, close
and relaunch only Vantage; do not kill unrelated processes. Use `open` with the verified
path. Check permission/signing/security errors.

If graphical interaction is unavailable, use the strongest combo of build, launch, API
checks, logs, screenshots. Mark only unobservable behavior as unverified.

## 9. Priority P1/P2 — implement the real attachment pipeline

Do not mark complete merely because upload succeeds or `not_processed` is returned.

Inspect the attachment flow, secure storage, tool interfaces, provider adapters,
dependencies, size limits, MIME handling, tests.

Implement usable safe processing per type:

- PDF: extract usable text from real PDF input, report encrypted/unreadable truthfully,
  avoid loading enormous files into memory.
- Images: use the established vision provider interface where configured; handle unsupported
  formats and provider errors.
- Audio: transcription/processing through a supported local or configured provider.
- Other: explicit supported extraction strategies; reject unsupported types truthfully.
- Preserve secure storage, ownership checks, size limits, safe names, cleanup, authorization.
- Distinguish stored, parsed, processed, unavailable states.
- Never claim an attachment was understood when it was only saved.

Write genuine tests using fixtures, realistic sample docs, mocked providers, failure cases.
Use real local fixture processing where possible without live credentials. Complete the
pipeline incrementally by format.

## 10. Priority P2 — calendar, email and browser workflows

Inspect existing implementations before adding abstractions.

Calendar: create/find/update/cancel through supported providers; validate dates/timezones/
titles/ambiguous requests; distinguish attempted write, server acknowledgment, verified
persisted result; test supported local + remote provider paths.

Email: secure connect through the existing supported provider; search/read/summarize; save or
label where supported; distinguish draft from sent and require confirmation for sending;
handle expired credentials, permissions, rate limits, provider failures.

Browser/ChatGPT: inspect existing automation/browser adapters; use a supported automation
mechanism to open an authorized target, enter authorized text, retrieve visible output if
supported; verify the result vs reporting success from a click/keystroke; prevent untrusted
page text from overriding Vantage policy or exposing credentials.

Use mocks for regression tests; verify live integration when configured accounts/permissions
permit. Do not invent connected services.

## 11. Priority P2 — finish iOS and Android companions

Do not declare finished because listed as paused or because sources exist. Proceed with
feasible companion work unless a hard restriction or unavoidable external dependency prevents
it.

iOS: build the real target with the supported toolchain; run the full available test suite;
fix compile errors and runtime defects; verify auth, API contracts, loading/error/recovery.

Android: inspect SDK/platform/build-tools/JDK/Gradle wrapper/Kotlin versions; fix
reproducible setup/build problems with compatible versions; run actual unit tests (not just
compile); build the APK; use an available emulator/device to install, launch, test API
connectivity; if none installed, investigate whether one can be launched with installed
tools. Do not silently declare device verification successful if unavailable.

Run every verification step available in the present environment, even when one platform is
blocked.

## 12. Priority P2 — release and developer usability

Verify build/run/install instructions against the real repo; improve missing diagnostics and
actionable errors; confirm API and UI contracts align; review the current `.app` artifact
and established release packaging; implement and verify a local packaging artifact if
supported and safe; do not publish/distribute without authorization; keep changed files
scoped; inspect for secrets/accidental generated files/unrelated edits; do not commit or push
unless authorized.

## 13. Test discipline

For every fix: reproduce the defect or add a test demonstrating intended behavior; implement
the minimum correct change; run the focused regression test; run adjacent tests; inspect the
diff; run static analysis where configured; build + exercise runtime when behavior changes;
record command and result.

At milestones: run full backend suite, dashboard checks, native macOS build/tests, iOS
build/tests, Android build/tests.

A test-count change is not proof of correct behavior. Investigate meaningful failures. Do not
fix brittle tests by weakening their contract. A signature check is not proof a GUI behaves.

Use existing project tools; do not install an unnecessarily large toolchain before checking
a smaller focused verification.

## 14. Priority selection on every iteration

A. Read the canonical tracker and Git state.
B. Identify the highest-priority incomplete acceptance criterion lacking real verification.
C. Prefer a task completable end-to-end now.
D. Inspect implementation; reproduce failure or establish missing evidence.
E. Make an actual source/test/runtime/build/integration improvement.
F. Verify with real commands or observations.
G. Inspect changes; preserve unrelated work.
H. Record evidence; identify the next incomplete criterion.
I. Continue the next task while capacity remains.

Priority order: P0 (data safety, authorization, startup, auth, backend correctness/security);
P1 (live conversational response, provider config, native app behavior, attachment pipeline);
P1/P2 (dashboard + end-to-end integrations); P2 (companion apps, release packaging,
regression hardening, docs/polish).

## 15. No false completion

Do not accept "all unblocked work is done" as a stopping condition. Remaining until
re-verified against the actual product: real provider inference through the app-facing
Command Center; fresh macOS launch + visible runtime acceptance; the actual attachment
pipeline; iOS build/test status; Android build/test/install; real end-to-end
calendar/email/browser; release artifact + setup verification.

Use a real acceptance matrix, not assumptions from source or old logs.

A task is VERIFIED only with observable evidence for its acceptance criteria. A missing
credential can block a live-provider test but not code implementation, mock testing, error
handling, other formats, other platforms, or unrelated features.

Use BLOCKED only after attempting the task and capturing the actual dependency; continue
unrelated work immediately.

Do not keep the loop alive by manufacturing pointless work once every authorized task is
genuinely complete; conversely, do not stop while useful unfinished work exists.

## 16. Required definition of done

Report complete only after: backend suite + quality checks run and recorded; native macOS
app rebuilt from current source and launched; core behavior exercised, not merely compiled;
Command Center response path verified with a real configured model or explicitly a
live-credential blocker after all achievable code/tests; provider config + error handling
tested; attachment processing implemented per supported format or each format has an
evidence-backed external dependency; calendar/email/browser have evidence-backed acceptance;
iOS and Android have undergone all builds/tests the environment supports; security,
regressions, changed-file scope, release instructions checked; every remaining item described
with exact evidence, not a vague "blocked".

If external dependencies prevent complete end-to-end verification, label the project
PARTIALLY COMPLETE and identify the exact dependency.

## 17. Mandatory first iteration

1. Inspect repository state and active test/build processes.
2. Confirm the current version and status of the native app.
3. Reconcile the old log with current files and live processes.
4. Identify the most important unfinished acceptance criterion.
5. Implement or verify a concrete behavior.
6. Run relevant tests/build/runtime checks.
7. Inspect the real diff.
8. Record evidence and move to the next unfinished task.

Do not spend the first iteration reproducing the previous recovery report. Do not finish the
first iteration with a plan only.
