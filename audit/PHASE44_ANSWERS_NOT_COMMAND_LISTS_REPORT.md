# Phase 44 — answers, greetings, and a question that was not a shell command

## 1. What was asked

> so if i have to work with it remember all commands make it like chatgpt, gemini etc like
> what i say they understand and work according to that
>
> add ai features also when i ask qns it give ans etc, like trains, common qns etc

The premise to fix first is in the sentence they pasted: *"I understood the words but not the
goal yet. Try things like…"* is what most of what a person naturally says currently returns.
So the nine sentences they would actually use were run through the router before anything was
written:

| Sentence | Measured before |
|---|---|
| `what is a train` | not_understood |
| `who is apj kalam` | not_understood |
| `explain how wifi works` | not_understood |
| `tell me about the moon` | not_understood |
| `what is the capital of France` | not_understood |
| `hello` | not_understood |
| `thanks` | not_understood |
| `what trains run from mumbai to pune` | **`run_allowlisted_command`** `{command: "from", args: "mumbai to pune"}` |
| `how do I run a marathon` | **`run_allowlisted_command`** `{command: "a", args: "marathon"}` |

## 2. The one that matters: a question was parsed as a command to execute

`_run_command` matched `\brun\b` **anywhere** in the sentence. So an ordinary question about
trains became a request to execute a program called `from`, and a question about exercise a
request to run `a`. The allowlist gate stopped both — that is the only reason this is a routing
bug and not an incident — but a question must never reach an executor, and every "what does
this do" answer in this repo is "whatever the gate allowed", which is not an answer about the
question.

Fixed by requiring the shape the route is for: imperative, anchored at the start (after an
optional *please / can you / could you / now*), and the command token must not be an ordinary
grammar word. Two properties pinned by tests so the fix cannot become the next bug:

* `run ls -la /tmp` still reaches `run_allowlisted_command` — the route still exists.
* the permission verdict for a real run is unchanged by this phase: a gate that gets quietly
  softer while "routing is being fixed" is the failure mode this project has the most of.

## 3. Answers, with no model and no key

`ask_question` reads the summary of a Wikipedia article, names the article in the reply, and is
a READ with no confirmation — the same tier as `research_brief`, which has been fetching papers
all along. Routes are deliberately few and shaped: *what is X*, *who is X*, *explain X*, *tell
me about X*, *define X*, *meaning of X*. A subject containing *my / I / project / battery /
today* is **not** sent to an encyclopedia: that question is about this machine and the specific
routes above answer it.

Live on this machine, real network, real articles:

```
what is a train   ok=True   "A train is a series of connected vehicles that run along a
                            railway track and transport people or freight. …"
hello             "Hello. I am Vantage, on your laptop and listening over your wifi."
thanks            "Anytime."
what trains run from mumbai to pune
                  the rules' own not-understood sentence — and NOT a shell command
```

**Where it stops, said in the answer.** `who is apj kalam` first returned *"I could not look up
'Apj kalam': HTTP Error 404: Not Found"* — the article is titled *A. P. J. Abdul Kalam*, so the
naive version of this feature answers textbook words and almost no real question. A lookup that
misses now searches first and follows the best title, which is a second network read with the
same deadline, not a guess: if the search finds nothing, the reply names the topic and says
there is no answer. Measured after that change: `apj kalam`, `mumbai`, `photosynthesis` all
`ok=True` with the article URL in the sentence.

This does **not** reason. "What trains run from Mumbai to Pune" has no article and this phase
does not pretend otherwise — that is a timetable, and the honest answer today is the rules' own
sentence. Reasoning is the model's job, and the model needs the key (Phase 42).

## 4. Greetings

`hello`, `hi`, `hey`, `thanks`, `thank you`, `ok`, `good morning` are answered like one —
at most three words, no action, nothing executed, `actions == []` asserted by a test. Small,
and the absence was the most visible way the system announced it had been built out of
capability lists and never talked to.

## 5. What this is not, yet

"Make it like ChatGPT" is mostly about *phrasing*, and 47 anchored rule shapes is still a
phrase list. The difference between this and what they asked for is a model that reads an
unfamiliar sentence and maps it onto registered tools — which is exactly what Phase 42 built
the safe container for, waiting on a key. And the microphone is waiting on ears (Phase 43).

## 6. Numbers

```
new tests        13
full suite       1846 passed · 0 failed · 0 xfailed · 0 skipped
tools            134 (read 71 · low 35 · external 13 · high 12 · physical 3)
planner routes   47 (was 45; the guard caught the published 45 → 47 again)
API routes       108 handlers / 106 distinct paths
test modules     71          reports 46
size             62,576 lines of versioned Python/HTML/JS (39,252 source · 23,324 tests)
safety matrix    6 of 6      acceptance commands measured 17 of 18
```

## 7. Carried forward

* **44-A** an answer is one paragraph of an encyclopedia's own opening, not a synthesis of the
  article. Long or comparative questions ("difference between TCP and UDP") get one side.
* **44-B** no multilingual lookup: a question in another language is searched in English.
* **44-C** *what is the capital of France* resolves to the article "Capital of France", which
  is not the answer. A real answer needs either a reasoning model or a data question — recorded
  rather than patched with more regex.
* **44-D** the search fallback costs a second network read on every miss (6 s deadline,
  sentence budget on top of it).
* Unchanged: 43-A…D, 42-B…D, 41-B…F, 40-A…E, 39-A…E, 38-A…D, 37-A…E, 36-A…E, 35-A…C, 34-A…C.

## 7b. The front door, corrected

The sentence a person sees when nothing matched was written before half of this existed, and it
listed five abilities while omitting the four they had just asked for -- which is how
"make it like ChatGPT" turns into "I have to memorise commands". It now says they do not
have to remember a command list, and names what works today: `what is a train`,
`open github.com`, `check storage`, `open my project`, alongside the old ones. A test
pins those four, so the hint cannot silently age again the way it just did.

## 8. Git

Committed on `main`, not pushed. Files: `jarvis/core/planner.py`, `jarvis/tools/knowledge.py`,
`jarvis/tools/__init__.py`, `tests/test_phase44_answers_not_command_lists.py`, `README.md`,
`benchmarks/*`, `assets/*`, this report.
