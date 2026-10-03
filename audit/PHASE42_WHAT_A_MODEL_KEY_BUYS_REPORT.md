# Phase 42 — what a model key actually buys

## 1. What was asked

> if possible connect gemini, apis i give you but i want ai system so i can control laptop
> from mobile, with voice, etc

Before asking anyone for a key, the existing model path was measured. `jarvis/config.py` has
`llm_base_url` / `llm_api_key` / `llm_model`, `jarvis/gateway/router.py` builds a real
OpenAI-compatible request with a `Bearer` header and a JSON mode, and `/health` reports
`llm_enabled`. It looks like a wired-up feature.

A local HTTP server was started on `:8798` that logs every path it is called on, the app was
run against it with a fake key, and three sentences were sent over HTTP:

```
/health                                    llm_enabled: true
POST /v1/chat/completions   received        0
POST /v1/embeddings         received        2
```

**No sentence ever asks the model anything.** `ModelRouter.complete()` has no caller anywhere
in `jarvis/` -- the only thing that reaches out is memory's embedder. So a key today buys a
status badge and an egress path for memory text, and changes nothing about how well the
system understands a sentence. That is the gap this phase closed, because it is the difference
between "I connected Gemini" and "the assistant got better".

## 2. The contract: it may speak, it may suggest, it may not act

`JarvisCore._advise_from_model` runs when, and only when, the rule router returned
`not_understood` and a model is configured.

| Property | Enforced by |
|---|---|
| A sentence the rules already answer costs **zero** model calls | `test_a_sentence_the_rules_understand_costs_no_model_call`, plus the live log above |
| A model's words never become actions | `response.actions == []`, and the executor is never reached (`test_a_model_suggestion_is_never_executed`) |
| No approval card is raised for something nobody asked for | `response.needs_confirmation` stays false -- a card for an unrequested action is theatre, not a gate |
| The answer is attributed | the reply names the model, and `model_used` becomes `gemini-2.5-flash` instead of `rules` |
| The ledger says so | the audit row carries `stage: model` and the model name, so the chain never reads as if the rules understood |
| A suggestion is reported, not obeyed | "It suggested `delete_file`. I did not run anything on that -- ask me for it yourself and it goes through the usual approval." |
| No key, no pretence | the rules' own sentence, `model_used: rules`, and the router is never called |
| A key that does not answer says so | "I also asked the model you configured (gemini-2.5-flash), and it was unavailable" |

The model's sentence is **appended** to the rule router's own answer rather than replacing it.
If it replaced it, the reply would read as though the system understood the sentence, which is
a claim about the pipeline and not true -- the state on screen stays NOT RECOGNISED, which is
what the rules actually did.

## 3. Live, after the change

Same fake endpoint, same two sentences, over HTTP on a scratch home:

```
what is my battery        model_used: rules              chat calls: 0
                          "Your laptop battery is at 100% (fully charged)."

wobbleth from glintar     model_used: gemini-2.5-flash   chat calls: 1
                          "I understood the words but not the goal yet. Try things like …
                           gemini-2.5-flash said: {"tool":"none"}
                           I did not run anything on its answer."
```

The gibberish sentence was chosen deliberately: it still reaches the rules' honest sentence,
and the model's own words are shown verbatim, including when they are useless.

## 4. What to do with a real Gemini key

Gemini publishes an OpenAI-compatible endpoint, which is the shape this router already speaks,
so no code is needed to point at it:

```
JARVIS_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
JARVIS_LLM_API_KEY=<your key>
JARVIS_LLM_MODEL=gemini-2.5-flash
```

Three things are said plainly rather than discovered later:

* **Text leaves the machine.** Memory embeddings and every advised sentence go to that host.
  The whole build otherwise runs with no network; this is the opt-in.
* **The embedder is configured separately** (`JARVIS_LLM_EMBED_MODEL`, default
  `text-embedding-3-small`) and that model name is not Gemini's. If Gemini refuses it, memory
  falls back to the dependency-free local embedder and keeps working -- verified by
  `jarvis/memory/embeddings.py`'s own fallback, not by a claim here. The live combination has
  not been run, because no key exists yet.
* **`llm_enabled` means configured, not reachable.** A wrong key still reports `true` and the
  advice path quietly degrades to "it was unavailable". Recording a probe is left open (42-B).

## 5. Numbers

```
new tests        7 (the model's job, its limits, and its absence)
full suite       1827 passed · 0 failed · 0 xfailed · 0 skipped
tools            133 (read 70 · low 35 · external 13 · high 12 · physical 3)   unchanged
planner routes   45                                                            unchanged
API routes       108 handlers / 106 distinct paths
test modules     69          reports 44
size             62,074 lines of versioned Python/HTML/JS (39,131 source · 22,943 tests)
```

No tool and no route was added: this phase gave the existing pipeline a second opinion, not a
new capability to act on.

## 6. Carried forward

* **42-A — voice from the phone is not built.** `/voice/transcribe` accepts an uploaded clip
  and `/phone` has no microphone, so today the voice path on a handset is the keyboard's own
  dictation button, which works and is not the same thing. A recorder → transcribe → command
  loop needs a working STT backend, and on this machine macOS speech is TCC-blocked and no
  whisper binary is present, so the honest options are the model key (a hosted
  transcription) or shipping a local model. Not started here rather than half-built.
* **42-B** `/health`'s `llm_enabled` is "configured", so a bad key looks like a working model.
* **42-C** the advice call has a 20 s deadline of its own inside the 60 s sentence budget, but
  it is still a *sequential* extra hop on every unrecognised sentence; a model that answers in
  8 s makes the honest reply arrive in 8 s.
* **42-D** the model cannot yet be asked to *summarise something the system has read* (the
  "give summary of that data" ask). 41-B remains open; this phase only gave it a route in.
* Unchanged: 41-B…F, 40-A…E, 39-A…E, 38-A…D, 37-A…E, 36-A…E, 35-A…C, 34-A…C.

## 7. Git

Committed on `main`, not pushed (the reader asked for files, and 39-F stands). Files:
`jarvis/core/orchestrator.py`, `tests/test_phase42_the_model_has_a_job.py`, `README.md`,
`benchmarks/*`, `assets/*`, this report.
