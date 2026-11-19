# V26.2 live backend evidence (port 8891, JARVIS_HOME=/tmp/v262)

## 1. Real command execution (/command)
POST /command {"text":"open Calculator","session_id":"v262-s1","source":"voice"}
-> 200 in 0.279s
   intent: open_app
   mission_id: MIS-faa5300139df
   decision: EXECUTE
   action: open_application ok=True verified=True
   reply: "Calculator is open on your macos machine."

Independent check: pgrep -x Calculator -> pid 7527
/System/Applications/Calculator.app/Contents/MacOS/Calculator
=> the claimed verification matches the real process table.

BUT the mission record reads: state=partially_completed verdict=unknown
steps=[('S1','open_application','pending')]
   /command creates the mission as a ledger stub and executes inline; it never
   calls MissionRunner, so no step/condition is ever recorded.

## 2. Real MissionRunner path (/missions/desktop -> /run)
POST /missions/desktop {"request":"click 400 300"} -> 200, status=planned
   mission MIS-cf0fb275c42e state=draft, step S1 desktop_click pending (x400,y300)

POST /missions/<id>/run {} -> 200
   state=awaiting_approval verdict=unknown
   step S1 still "pending"
   detail: "requires approval before it may run (level 3 (destructive) action
            requires confirmation); a mission does not supply that approval;
            confirmation CONF-c5b5e2355b77a0abbc23955d2aff5c3e"
=> the permission gate parked the mission; nothing executed.

## 3. Denial
POST /missions/<id>/cancel -> 200, state=cancelled
POST /missions/<id>/run  {} -> 404
   "cancelled -> awaiting_approval is not a legal transition
    (from cancelled the legal targets are: none)"
=> a cancelled mission cannot be resumed into execution; the state machine refuses.

## 4. Three-turn contextual conversation (isolated backend, flag ON)
session ctx-8294, all via POST /command

T1 "open Calculator"  -> intent=open_app, verified=True,
     "Calculator is open on your macos machine."
     Independent: pgrep -x Calculator -> pid 7527

T2 "is it running?"   -> intent=check_agent_status
     "Calculator is installed (version 12.0) and is running."
     action verified=False, verification=unknown
     => "it" resolved to Calculator from T1. The state report is real.
     DEFECT (cosmetic): the composer appends "but I could not verify it
     happened" to an informative answer, producing a self-contradictory
     sentence. The underlying fields are honest; the composed string is not.

T3 "close it"         -> intent=close_app, resolved to Calculator
     "Quit Calculator, but I could not verify it happened."
     Independent: pgrep -x Calculator -> gone
     => "it" resolved again; the action really happened. verified=False is
     honest (close_app does not re-check the process table), so "could not
     verify" is true even though the quit succeeded.

## 5. Denial erasure -- DEFECT FOUND AND FIXED
Before fix (reproduced live): deny -> transition running -> /run
   denial text absent from mission entirely, step back to "pending",
   fresh confirmation minted and accepted -> executed the denied action.
After fix (commit c2ddb7d, live re-run):
   state=blocked, step=failed, detail="denied by the operator",
   no confirmation minted.
Guard mutation-tested: `if denied:` disabled -> test fails; restored -> passes.

## 6. Feature flag A/B (same sequence, two profiles, two ports)

Flag OFF (JARVIS_HOME=/tmp/v262off, :8892)
  "open Calculator" -> intent=open_app, "Calculator is open on your macos machine."
  "close it"        -> intent=unknown  (pronoun NOT resolved -- old behaviour)

Flag ON  (JARVIS_HOME=/tmp/v262, :8891)
  "open Calculator" -> intent=open_app
  "close it"        -> intent=close_app, "Quit Calculator, but I could not verify it happened."
                       pgrep -x Calculator -> closed

=> the flag is the only difference; ordinary commands behave identically with it off.

## 7. Backend route paths -- an architectural finding

/command (and /voice/command -> VoiceSession -> core.handle) executes actions INLINE
in the orchestrator and creates the mission only as a ledger STUB: the mission
record ends up state=partially_completed verdict=unknown with its step still
"pending", because MissionRunner is never invoked on this path.

MissionRunner is constructed only at:
  jarvis/api/missions_routes.py:478   (POST /missions/{id}/run)
  jarvis/core/skill_runner.py:196

So "Voice Operator -> MissionRunner" is NOT the pipeline for /command. There are
two real paths:
  A. /command ................ inline execution + ledger stub; no step/condition recorded
  B. /missions/desktop -> /run MissionRunner: parks for approval, records steps and
                                conditions, verifies

The Voice Operator UI calls ConversationService.send -> /command, i.e. path A.
If the campaign requires path B for voice, that is a wiring change not yet made.

## 8. Native desktop executor

The worktree has no native/desktop_executor/vantage-desktop (it is gitignored
build output; the main checkout has a 245528-byte binary). A desktop_click step
that passed approval therefore failed with the honest message "the native
desktop executor is not built". No click was faked.

## 9. Voice -> MissionRunner (JARVIS_COMMAND_MISSION_MODE)

Flag OFF (unchanged):
  open Calculator -> "Calculator is open on your macos machine."
  mission: partially_completed / verdict unknown / step pending / 1 condition, unchecked

Flag ON:
  open Calculator -> "Done, and I checked it: calculator is running."
  mission: completed / verdict verified / step done / condition verified
           observation "calculator is running"

  close Calculator -> "Done, and I checked it: calculator is no longer running."
  mission: completed / verified / "calculator is no longer running"

  delete /tmp/vm3/nonexistent-but-protected.txt
     -> decision CONFIRM, needs_confirmation True,
        "This needs your approval before it runs."
        mission awaiting_approval, step pending, nothing deleted
     -> after supplying the parked confirmation to /run:
        state failed, step failed, "Nothing to delete at ..."
        (the file did not exist; the failure is the truthful outcome)

Verifier discrimination (direct calls):
  observe_process("zzz-not-a-real-process-zzz")             -> failed
  observe_process("zzz-...", absent=True)                   -> verified
  observe_process("Finder", absent=True, grace=1.0)         -> failed  ("Finder is still running")

Defect found during this work: close_app returns as soon as the quit is sent, so
verifying immediately reported "calculator is still running" and failed a
condition that had been satisfied. observe_process now takes a bounded grace
window for the absent case; the wait only ends on the process actually being
gone, so it cannot turn absence into a pass.

Mutation-tested: removing the observer guard, and removing the grace window, each
fail a test. The first mutation initially survived because the test used a tool
with no `app` param and was exercising the wrong guard; corrected to
open_document.
