# Phase 55 — a search that searches

| | |
|---|---|
| Phase | 55 |
| Date | 2026-10-04 |
| Starting commit | `377e79f` (Phase 54 close-out) |
| Ending commit | `176bcfb` — filled in at the Phase 56 close-out; the header promised "recorded in §8" and §8 never recorded it |
| Tests before | 2155 passed · 0 failed · 0 skipped |
| Tests after | **2190 passed · 0 failed · 0 skipped** |
| Tools before / after | 141 / **142** |
| Planner routes before / after | 51 / **52** |
| HTTP surface | 117 / 111 paths — unchanged, no endpoint was added |
| Confirmation-gated | **28 of 142** |
| **§6 coverage** | 36 / 37 → **37 / 37** |
| Push status | **nothing pushed; local commit only** |

Gap D of the operating prompt: `search Google for FastAPI` was §6's last utterance without a
capability, and the tempting fix — route it at `open_url` with `https://www.google.com` — is a
lie that renders perfectly: a browser opens, a page loads, nothing was searched, and the reply
reads like a success.

## 1. Measured first

```
search Google for FastAPI        -> not_understood
search the web for X             -> not_understood
search files for my report       -> search_files
search my email for the invoice  -> declined
open Google                      -> open_url https://www.google.com
```

and, deciding the risk tier by measurement rather than by intuition:

```
gated tools by risk tier: {1: 1, 2: 13, 3: 12, 4: 2}
```

**Every one of the 13 EXTERNAL tools is confirmation-gated.** So in this registry EXTERNAL means
"changes something outside", and a search — which changes nothing — does not belong there. Its
closest precedent is `open_url`: reaches out, reads, LOW, ungated. `search_web` follows it. A
search behind a per-query approval is a search nobody uses, and the gate would not have made
anything safer.

## 2. What was built

| file | what it is |
|---|---|
| `jarvis/websearch.py` | the provider: URL building, the bounded fetch, the parse, and the two failure types |
| `jarvis/tools/websearch.py` | `search_web` — maps those outcomes to states, and refuses before the network when the query is empty or a pasted paragraph |
| `jarvis/core/planner.py` | `_web_search`, registered before `_search_files` (52 routes) |
| `tests/test_phase55_a_search_that_searches.py` | 35 tests |

Provider: DuckDuckGo's Instant Answer API. No key, documented, JSON, and genuinely empty for
many queries — which is the point of having a distinct empty state.

## 3. Three outcomes, kept distinct

```text
answered with something     -> results, and the source is named
answered with nothing       -> ok, count 0, "an empty answer from one source,
                               not a claim that nothing exists"
could not be reached/read   -> FAILED: "This is the network, not an empty result."
```

The middle and the last must not collapse into each other — that is the P1-5 rule this project
already applies to mail and calendar, where an unreachable server was being reported as an
empty day.

## 4. The query is data

`build_url` puts the query through `urlencode` with the other parameters, so it can arrive as a
value and nowhere else. The test asserts the parsed contract rather than pattern-matching the
string:

```python
assert query.get("q") == ["a&format=xml&redirect=https://evil.example"]
assert query.get("format") == ["json"]
```

**My first version of that assertion was wrong and the code was right.** I asserted
`"evil.example" not in url` — but the host appearing *inside* the encoded value is correct
behaviour; it is the text the user asked about. The defect would be it arriving as a second
parameter or as the request's host, which is what the fixed assertion now checks.

Live, with a query shaped like an attack: `a"b&c=1 redirect=https://evil.example` went out as
one encoded value and came back as an honest empty answer.

## 5. Live

Isolated instance, real network, no mocks.

```
search Google for FastAPI
  -> "duckduckgo-instant-answer had no result for 'FastAPI'. That is an empty answer from
      one source, not a claim that nothing exists."                        281 ms
search the web for the heat death of the universe
  -> "Searched for 'the heat death of the universe': 2 result(s) from
      duckduckgo-instant-answer. The Heat Death of the Universe —
      https://en.wikipedia.org/wiki/The_Heat_Death_of_the_Universe …"      143 ms
```

The first is the interesting one: `FastAPI` genuinely has no instant answer, and the tool said
so instead of inventing a summary or quietly opening a browser.

The outage path, against a black-hole address (a real connect attempt, not a patched function):

```
outage path: ok=False in 5.01s
  "I could not reach the search service, so I have no answer for 'anything'.
   This is the network, not an empty result."
```

5.01 s is inside the 5 s budget the module declares — Phase 34's one-sentence deadline held
against a host that never answers, which is the case that used to produce 20-second sentences.

## 6. Two wrong expectations of mine

1. `search Google for FastAPI` → I expected the query to come back as `FastAPI` and the planner
   produced `fastapi`, because the rule matched the lowercased text. **The product was wrong
   here**: the query is what the person said, and case matters to a search. Fixed by matching
   on the sentence as spoken.
2. I asserted `what is a train` reaches `search_papers`. It reaches `ask_question`, and it did
   so before this phase too — checked against Phase 53's committed planner rather than argued.
   The expectation was invented from the help text's example list; the test now records the
   measured behaviour and says why it is legitimate.

## 7. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 2155 passed | **2190 passed · 0 failed · 0 skipped** | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 55 tests | 0 | **35** | `tests/test_phase55_a_search_that_searches.py` |
| Test modules | 81 | **82** | `ls tests/test_*.py \| wc -l` |
| Phase reports | 56 | **57** (this file) | `ls audit/*.md \| wc -l` |
| Lines | 67,882 — 41,174 · 26,708 | **68,461 — 41,430 source · 27,031 tests** | `wc -l` |
| Tools | 141 | **142** | registry |
| Risk spread | 76 / 37 / 13 / 12 / 3 | **76 / 38 / 13 / 12 / 3** | registry — the new tool is LOW |
| Planner routes | 51 | **52** | `len(core.planner._rules)` |
| Confirmation-gated | 28 | **28** | unchanged |
| **§6 coverage** | 36 / 37 | **37 / 37** | all 37 utterances re-run through the planner |

## 8. Open items

* **55-A** One source, and an encyclopaedic one. `search Google for FastAPI` returns nothing
  because the Instant Answer API has no article for it — a ranked web index would answer
  differently. The message says which source answered, so the limit is visible rather than
  implied.
* **55-B** No key-based provider (Brave/SerpAPI/Google CSE) is wired, so there is no ranked
  result path at all. That is a provider decision, and §I of the operating prompt puts provider
  work behind this one.
* **55-C** Results are returned as text in the reply; nothing renders them as links in the phone
  or panel yet.
* **55-D** The 5 s budget is per search. A slow-but-alive service is indistinguishable from a
  dead one at that boundary, and the message says "could not reach" in both cases.
* **55-E** `search files for my report` yields the query `"files for my"` — a pre-existing
  defect in `_search_files`'s capture, noticed while writing the boundary test for this phase
  and left alone because it is a different capability's bug.

## 9. Next

Phase 56 — the first-run permission wizard: a deliberate, user-initiated request path.
`/permissions` reads state and must stay that way; requesting must never be a side effect of a
poll.
