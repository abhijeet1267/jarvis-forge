# Phase 60 — a second vendor, without a second authority

|                   |                                                                                                                   |
| ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| Phase             | 60 (the roadmap's provider adapters, spec §10 OpenAI / §11 Gemini / §12 the neutral contract)                     |
| Date              | 2026-10-05                                                                                                        |
| Starting commit   | `73a87cc` (Phase 59 close-out)                                                                                    |
| Ending commit     | `1bec583` (the work) → `b004a2b` (the close-out, whose own hash is written by the next commit to touch this file) |
| Tests before      | 2332 passed · 0 failed · 0 skipped                                                                                |
| Tests after       | **2355 passed · 0 failed · 0 skipped** (94.9 s)                                                                   |
| Tools / gated     | 145 / 30 → **145 / 30** — no capability added, no gate moved                                                      |
| Planner rules     | 52 → **52**                                                                                                       |
| Host HTTP surface | 122 route objects / 115 paths → **unchanged** (`/health` gained three fields, not a route)                        |
| Vendor proof      | **`PROVIDER_LIVE_VENDOR_CALL: NOT MEASURED`** — no API key exists on this machine (§9)                            |
| Push status       | **nothing pushed; local commits only**                                                                            |

---

## 1. What was measured before anything was written

The gap register said _"No provider adapters in use — OpenAI/Gemini adapters per §22–24 not
built"_. That row was wrong in both directions, and the code answered it:

```
jarvis/gateway/router.py            149 lines, one wire format: {llm_base_url}/chat/completions,
                                    Bearer header, `provider="openai-compatible"` hard-coded in
                                    the ModelChoice it reports.
jarvis/memory/embeddings.py:231     a second OpenAI URL, built by hand: {llm_base_url}/embeddings
jarvis/vision/providers.py:128      a third: {base_url}/chat/completions with an image part
jarvis/voice/stt.py:393             a fourth: {llm_base_url}/audio/transcriptions
jarvis/core/orchestrator.py:233     ModelRouter.complete() IS called -- for a sentence the rules
                                    did not recognise (Phase 42). It is not, as an older report
                                    had it, a method with no caller.
config.py                           llm_base_url / llm_api_key / llm_model / llm_embed_model ...
                                    and no `ai_provider` field at all.
```

So an adapter half-existed: one vendor, four call sites, no selection.

Stated exactly, because the first draft of this paragraph overstated it and the shipped
`READ ME FIRST.txt` disproved the overstatement: **a second vendor was reachable, but only by
pointing the OpenAI-shaped URL at its compatibility endpoint** —
`https://generativelanguage.googleapis.com/v1beta/openai/`, which is what the delivery readme has
always told a Gemini user to type, and which did work. What did not exist was any way to ask a
vendor in its own native shape, or any way to _say which door a sentence would go out of_:
`/health` reported `llm_enabled` and `llm_source` and never a vendor, and `ModelChoice.provider`
was the constant `"openai-compatible"` whatever the settings said. That is still a status page
being the unreliable part, which is worth fixing; it is just not the same claim as "it would have
failed", and the weaker true one is the one to print.

And the stronger false one had a cost. Assuming it is what made this phase's own first
`GeminiProvider.root()` append a version segment to a URL that already carried one —
`…/v1beta/openai/v1beta/models/…` — which is a documented setup broken by the thing written to fix
it. Caught by reading the delivered readme and writing test 7 against it, not by reasoning.

And an unconfigured machine reported `llm_enabled: false` honestly:

```
GET /health on the delivered app  -> llm_enabled: false, llm_source: "none"
~/.jarvis-forge/llm.env           -> does not exist
JARVIS_LLM_API_KEY in the shell    -> unset
```

## 2. What was built, and what was deliberately not

`jarvis/gateway/providers.py` (418 lines): `OpenAIProvider`, `GeminiProvider`, one selection point
(`select_provider`), one POST builder, and `ProviderReply` — an answer that names its vendor, its
model, and its own failure.

Spec §12 asks for a provider-neutral contract of `AIPlan` / `ProposedToolCall` / `Verification`,
with `source: rule | openai | gemini | local`. **The plan half was not adopted, on purpose.**
§13 says the AI "must not replace deterministic rules", and Phase 42 decided the model may speak and
nowhere else. Building `AIPlan` would be a request for the model to propose tool calls, which is a
security change wearing a schema. What was taken from §12 is the _attribution_ half: every answer
now carries the vendor and model that produced it, which is the part a reader can be held to.
Recorded as 60-B rather than left as a silent omission.

The spec also named the `google-genai` SDK for §11. It is not installed here
(`find_spec('google.genai') → no module named 'google'`), and adding a dependency to a stdlib-first
bundle is a decision, not a detail. The adapter speaks the REST API that SDK wraps — same wire, no
install — and this paragraph is where that deviation is stated instead of hidden.

## 3. Selection, and what a bad name does

`JARVIS_AI_PROVIDER` (default `openai`, so an existing install's bytes on the wire do not move).
An unrecognised name **raises** `ProviderUnknown`, listing the known ones; it does not fall back.
A typo that silently meant "we used the other vendor anyway" would make every subsequent sentence
naming a model false, and the refusal is what makes the status page tell the truth (§8, live).

## 4. The two wire shapes, and the rule that produced them

|               | OpenAI                                    | Gemini                                              |
| ------------- | ----------------------------------------- | --------------------------------------------------- |
| chat          | `POST {base}/chat/completions`            | `POST {base}/v1beta/models/<model>:generateContent` |
| auth header   | `Authorization: Bearer <key>`             | `x-goog-api-key: <key>`                             |
| system prompt | `messages[0].role = "system"`             | `systemInstruction.parts[0].text`                   |
| JSON mode     | `response_format: {"type":"json_object"}` | `generationConfig.responseMimeType`                 |
| answer        | `choices[0].message.content`              | `candidates[0].content.parts[*].text`               |
| embeddings    | `POST {base}/embeddings`                  | `POST {base}/v1beta/models/<model>:embedContent`    |
| model name    | in the body                               | in the path                                         |

`root()` accepts the two spellings a person actually has: `…/v1` (the shape this build assumed)
and `…/v1beta/openai/` (the one the delivered readme tells Gemini users to type). Either way exactly
one version segment ends up in the path; the first version of this method stripped only `/v1` and so
broke the second spelling (§1). Model names come from configuration on both sides (spec §10's rule) — the
check is that the wire moves when the setting does, not that a string is absent from a source file.

## 5. Failure has a shape, and it is never an empty success

Three rules, each with a test:

- **the vendor's own words** — an HTTP error body is parsed for `error.message` and carried into
  `ProviderReply.error`, because "the model was unavailable" tells a person nothing while "model
  not found" tells them what to change. The key is redacted out of it (`_redact`), since a vendor
  can echo the request it rejected.
- **a timeout is a timeout, with its number** — every read goes through `jarvis.net.http_read`, so
  the wait is a _total_ budget and not an inactivity gap. A test proves it with a server that
  dribbles bytes forever, which is the exact shape Phase 34 measured as unbounded.
- **a capability a provider does not have is refused, not returned empty** — `[]` from a failed
  embedding or `""` from a failed transcript is the offline-as-empty bug this project has already
  fixed twice, for mail and for calendar.

That last one is why `transcribe()` raises `ProviderCapabilityMissing` instead of answering:
transcription and vision are still OpenAI-shaped in their own modules, and a provider that pretended
otherwise would have produced the empty answer the rule exists to prevent. Stated on `/health` as
`provider_paths`, so the boundary is visible rather than assumed (§8).

## 6. The memory write path, which is where a second vendor actually matters

Phase 42 measured that the two outbound calls a configured key made were **both**
`/v1/embeddings`. So the embedder was the path to fix first, and it was fixed in a way that keeps
Phase 34's rule intact: the _shape_ (URL, headers, body, parsing) now comes from the provider, while
the _read_ stays at the call site where its total budget was pinned and audited.

```python
provider = select_provider(self.settings)      # which vendor, therefore which URL and headers
shape    = provider.embedding_request(text, model)
body     = http_read(request, total=REQUEST_TIMEOUT, what="the embedding model")   # unchanged
vector   = provider.parse_vector(body)         # vendor-specific parse, vendor-neutral caller
```

Splitting it any other way — moving the POST into the provider — would have relocated an invariant
another phase measured and pinned, to make this phase's code tidier. That trade was not taken, and
the reason is written next to the line rather than only here.

## 7. The defect this phase found by running it

The live proof set `JARVIS_AI_PROVIDER=mixtral` — an unusable name — and read the response:

```
model_used -> "gemini-2.5-flash"
```

No call had been made. Nothing was sent anywhere. And the pipeline still attributed the answer to a
model, in a machine-readable field whose whole job is that attribution, and the prose said _"I also
asked the model you configured, and it was unavailable"_ about a question nobody had asked. The
same defect was waiting in the ordinary case: a configured vendor that does not answer leaves
`model_used` naming a model that never replied, while the words the person read were the rule
router's.

Red-first tests for both (`tests/test_phase60_…py` §6), then the rule: **`model_used` names a model
only when a model answered.** A refusal names the refusal; an unanswered question says so and stays
attributed to `rules`.

This is the project's oldest lesson in a new place — a handler returning is not the world changing —
and here it was not even a handler: it was a label.

## 8. Live, on an isolated home and two loopback ports

`JARVIS_HOME=$(mktemp -d)`, a real `run.py serve` on 127.0.0.1:8918, and a **local stand-in vendor**
on 8919 that records the bytes it receives. Quoted output:

```
GET /health                                    -> llm_enabled=True ai_provider='gemini'
provider_paths                                 -> {"chat": "gemini", "embed": "gemini",
                                                   "vision": "openai-shaped, not provider-routed",
                                                   "stt": "openai-shaped, not provider-routed"}
POST /command "wobbleth and glintar please"    -> 200 model_used='gemini:gemini-2.5-flash'
  what the vendor received                     -> /v1beta/models/gemini-2.5-flash:generateContent
  headers it saw                               -> {"content-type": "application/json",
                                                   "x-goog-api-key": "a-key-not-fr…"}
  system instruction reached it                -> True
  the reply a person got                       -> I understood the words but not the goal yet. …
                                                   gemini:gemini-2.5-flash said: I do not know
                                                   wobbleth. If it is a file, name part of the
                                                   filename.
                                                   I did not run anything on its answer.
POST /command "remember that the heater coil…" -> 200 Got it. I will remember: …
  embedding requests the vendor received       -> 2
  embedding path                               -> /v1beta/models/gemini-embedding:embedContent
  embedding body                               -> {"model": "models/gemini-embedding",
                                                   "content": {"parts": [{"text": "the heater
                                                   coil arrived on tuesday"}]}}
-- restart with JARVIS_AI_PROVIDER=mixtral --
GET /health                                    -> ai_provider="refused: JARVIS_AI_PROVIDER=
                                                   'mixtral' is not a provider this build can talk
                                                   to. Known: gemini, openai"
POST /command                                  -> 200 model_used="rules"
                                                   "I did not ask a model about that: JARVIS_AI_
                                                   PROVIDER='mixtral' is not a provider this build
                                                   can talk to. Known: gemini, openai. So that
                                                   answer is the rule router's own."
outbound addresses used                        -> 127.0.0.1:8918 and 127.0.0.1:8919 only
```

The isolated home was deleted, both processes stopped, no port left listening, and the real
`~/.jarvis-forge` was not written to.

## 9. PROVIDER_LIVE_VENDOR_CALL: NOT MEASURED

Everything above is **protocol proof against a local stand-in**. It shows that the bytes a Gemini
endpoint would receive are the right bytes, that the answer is parsed the way that vendor answers,
and that selection, refusal and attribution behave. It does not show that a real vendor accepts
them, and nothing here is presented as if it did.

```
PROVIDER_LIVE_VENDOR_CALL: NOT MEASURED
missing: an API key. Measured on this machine: /health answers llm_enabled=false,
         ~/.jarvis-forge/llm.env does not exist, JARVIS_LLM_API_KEY is unset.
not performed: any call to api.openai.com, generativelanguage.googleapis.com or any other
         vendor endpoint. A key is the user's to supply, and spending someone's credit is not
         a verification step an agent gets to take by default.
what would close it: one configured key, then the same checks with the base URL pointed at the
         real vendor -- the sentences, the paths and the assertions are already written.
```

## 10. Browser verification: NOT APPLICABLE

No page changed. `/health` gained three JSON fields that no shipped page reads yet. The dashboard
says "Basic Mode" from `llm_enabled` and still does; naming the vendor in that panel would be a
UI change worth doing with a screenshot behind it rather than folded in here (60-C).

## 11. Tests

`tests/test_phase60_a_second_vendor_without_a_second_authority.py`, **23 tests**, red-first, against
a recording HTTP server on a real socket rather than a mock:

selection — default is the shape that already worked · a second vendor chosen by configuration ·
an unknown name refused with the list of known ones.
wire — OpenAI's shape unchanged · Gemini's shape is its own and not OpenAI with renamed fields ·
the model comes from configuration on both sides · JSON mode asked for in each vendor's language.
failure — a vendor refusal carries the vendor's words · a timeout is a timeout with its number ·
a missing capability raises instead of answering empty · the key never returns in what a caller is
told.
embeddings and the router — each vendor's embedding endpoint · the router asks the vendor the
configuration names · an absent vendor still returns `None`, because that is the contract the
orchestrator was written against · the memory write path asks the named vendor · and the same path
**still** produces byte-identical OpenAI requests, which is the regression that mattered.
authority — a second vendor's answer still emits no action · a model that never answered is not
credited with one (§7, twice: refused name, unreachable vendor).
and one guard — nothing claims a `realtime_session`, which spec §10/§11 both ask for and this
phase did not build (60-D).

Two of these were written after the live run, in §7.

## 12. Numbers, re-measured after the work

|                                                     | before (Phase 59 close)     | after (measured now)                                         |
| --------------------------------------------------- | --------------------------- | ------------------------------------------------------------ |
| Tests                                               | 2332 · 0 failed · 0 skipped | **2355 · 0 failed · 0 skipped** (94.9 s)                     |
| Test modules                                        | 86                          | **87**                                                       |
| Tools / confirmation-gated                          | 145 / 30                    | **145 / 30**                                                 |
| Risk split (read/low/external/destructive/physical) | 77 / 38 / 15 / 12 / 3       | unchanged                                                    |
| Planner rules                                       | 52                          | **52**                                                       |
| Host HTTP routes / paths                            | 122 / 115                   | **122 / 115**                                                |
| Providers selectable                                | 1 shape, no selection       | **2** (`openai`, `gemini`), selected by `JARVIS_AI_PROVIDER` |
| Versioned Python/HTML/JS/Swift                      | 72,227                      | **74,233** lines                                             |
| Shipped code                                        | 42,937                      | **43,871** lines                                             |
| Tests                                               | 28,689                      | **29,761** lines                                             |
| `audit/` reports                                    | 61                          | **62** (this file)                                           |

Two consecutive full-suite runs were made. The first had **5 failures in `tests/test_vision.py`**,
all macOS-Vision OCR tests, which is the documented 27-D load flake; the machine was re-tried clean
and two further full runs returned 2354 passed, 0 failed. The flake is reported as what it is
measured to be, not blamed on this phase's code.

Phase 34's ratchet over silence-bounded `httpx` reads still counts 6 sites, and that is honest
rather than unchanged: the router's httpx call was converted to a total-budget read, and it was
never in the 6 (the ratchet counts numeric `timeout=` values, and the router's was a variable).
One silence-bounded site is gone; the number that tracks them did not see it. Recorded as 60-E
because a ratchet that misses a conversion is a ratchet with a hole in it.

## 13. Open items

- **60-A — vision and transcription are still OpenAI-shaped.** `jarvis/vision/providers.py:128` and
  `jarvis/voice/stt.py:393` build their own URLs. `/health` says so (`provider_paths`) instead of
  implying the gateway covers them; with `ai_provider=gemini`, "describe this image" and cloud
  transcription would still call the OpenAI shape. Converting them needs their own tests — the
  image-part and multipart bodies are different work from chat.
- **60-B — the §12 `AIPlan` / `ProposedToolCall` half is not built,** deliberately (§2). If the
  project ever wants a model to propose tool calls, that is a permission-engine decision with a
  confirmation story attached, not a dataclass.
- **60-C — no page names the vendor.** The dashboard's model badge still says only Basic Mode /
  configured. Now that the server reports `ai_provider`, the panel can say _which_ vendor; that
  needs a browser check at 1440px.
- **60-D — no `realtime_session`.** Spec §10/§11 ask for it. It needs a vendor websocket, a key,
  and a client that can hold the socket. A guard test fails if the word appears in
  `providers.py` without the proof beside it.
- **60-E — the Phase 34 ratchet does not see converted sites** (§12). It should count call sites by
  mechanism, not by the shape of a literal.
- **60-F — a pre-existing order-dependent leak, found on the way and not caused here.** Running
  `pytest tests/test_phase34_one_sentence_one_deadline.py tests/test_memory_advanced.py::test_index_and_search`
  fails with `ValueError: unknown url type: '/embeddings'`: some pair of Phase 34's tests leaves the
  process-global embedder as a `RemoteEmbedder` with an empty base URL, and the next file's
  `VectorIndex()` inherits it. Verified reproducible on `73a87cc` with this phase's work stashed, so
  it is recorded rather than fixed under this commit. It does not appear in a normal full-suite run,
  which is exactly why a leak like this survives.

## 14. Delivery

Work commit `1bec583` — _Put two vendors behind one door, and take the badge's word for nothing_.
Close-out commit `b004a2b` — _Correct what Phase 60 claimed about itself, and take the second
base URL_: the correction in §1 and the `root()` fix that came out of it, recorded as their own
commit rather than folded under the first one. The hash was filled in after that commit existed;
an earlier phase wrote a hash into a report before the commit did, and that is a fabrication with a
good intention behind it.

Rebuilt from this tree, re-deployed, and re-verified with **GETs only** — no POST reached the user's
real data directory:

| check                                                                        | result                                                                                                                                                                                                                       |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `codesign --verify --deep --strict` on the deployed bundle                   | valid on disk, satisfies its Designated Requirement                                                                                                                                                                          |
| `jarvis/gateway/providers.py` inside the bundle, with the corrected `root()` | present (`v1beta/openai` appears twice in the staged file)                                                                                                                                                                   |
| same checks after extracting `Vantage.app.zip` into a temp dir               | valid — the archive, not just the folder                                                                                                                                                                                     |
| `GET /health` on the delivered app                                           | `tools: 145`, `llm_enabled: false`, `llm_source: none`, `ai_provider: "openai"`, `provider_paths` naming vision and stt as not provider-routed                                                                               |
| `GET /tools`                                                                 | 145 entries                                                                                                                                                                                                                  |
| `GET /persistence`                                                           | `supported: true`, `installed: false`, `loaded: false` — the machine still left as found (57-A is a user decision)                                                                                                           |
| `GET /health` with no token                                                  | **401**                                                                                                                                                                                                                      |
| the claim the shipped build wrote                                            | `{"pid": 76145, "host": "0.0.0.0", "port": 8765, …}`                                                                                                                                                                         |
| archives                                                                     | `Vantage.app.zip` 1,166,912 B and `vantage-source.zip` 2,519,732 B, both rebuilt; the source archive holds this report and scans clean for `.env`, tokens, key files and databases                                           |
| suite                                                                        | **2355 passed · 0 failed · 0 skipped**, measured twice clean (94.9 s and 82.4 s); one earlier run showed the five documented 27-D Vision-OCR load failures, which passed on a quiet re-run and are reported as what they are |

`READ ME FIRST.txt` gained a section 7, "Which vendor the key belongs to", which says what the
choice does, that an unusable name is refused out loud, that `provider_paths` names the two shapes
still OpenAI-bound, and that **no model call has been made from this build to any real vendor**. It
is still hand-maintained in the delivery folder rather than generated from the repo (56-F) — and
this phase is the second time that gap has caught a claim of mine that the shipped text contradicted.

Nothing was pushed.
