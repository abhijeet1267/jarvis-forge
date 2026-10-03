# Phase 45a — a key the app can be given without damaging it

## 1. The blocker was the place, not the mechanism

Phase 42 left the reader with a working model path and one instruction: put a key in an
environment variable. That works, and it is also the worst available answer, for a reason found
while writing it down:

* `Settings` reads `.env` from the **current working directory**.
* For the installed app the working directory is `Vantage.app/Contents/Resources/app`.
* So the natural file to edit is **inside the bundle** — and Phase 40 proved what a file added
  after signing does: `codesign --verify` → *"a sealed resource is missing or invalid"*, which
  on another machine is the difference between "Open" and "Vantage is damaged".

A key file that breaks the signature of the app it configures is not a feature. So the file
moved next to the data: `~/.jarvis-forge/llm.env`, mode 0600, outside the bundle entirely.

## 2. What the loader does, and in which order

`jarvis.config.apply_llm_file()` runs before `Settings` is constructed, because pydantic reads
the environment at construction time:

* `KEY=value` lines; a `JARVIS_` prefix is optional, so both `LLM_API_KEY=` and
  `JARVIS_LLM_API_KEY=` work.
* **The real environment wins.** A key exported in a shell is the one that process meant; a
  file silently outranking it is how a stale key outlives the thing it was written to replace.
* A comment, a blank line or a line without `=` is skipped rather than fatal — a typo in a
  hand-edited file must not stop the product booting.
* A file readable by anyone on the machine is **complained about and still honoured**:
  *"…is readable beyond its owner (mode 644) and holds a credential; run: chmod 600 …"*.
  Refusing to start over a permission bit is its own bad trade; saying nothing is worse.
* The warning never contains the key. There is a test for exactly that, because the tempting
  implementation prints what it found.

## 3. The status page now names its own source

`/health` gains `llm_source`: `"none"`, `"env"` or `"llm.env"`. This is the first half of 42-B,
which was filed as *"`llm_enabled` means configured, not reachable"*: the badge already hid the
difference between a real key, a typo, and an empty file, and "where did this come from" is the
next thing a person asks when the model does not answer. Reachability is still not claimed —
probing a paid endpoint on every status read is not an honest way to look available.

## 4. Measured live, both ways

A real process on a scratch home, with the file present and a clearly-fake key:

```
$ cat $JARVIS_HOME/llm.env            # chmod 600
JARVIS_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
JARVIS_LLM_API_KEY=clear-text-key-for-the-test
JARVIS_LLM_MODEL=gemini-2.5-flash

GET /health            llm_enabled: true   llm_source: llm.env
POST /command "wobbleth glorp"
  → "…I also asked the model you configured (gemini-2.5-flash), and it was unavailable,
     so that answer is the rule router's own."
```

That last line is the whole chain working end to end: file → config → the Phase 42 advice path →
a real HTTPS call to Google's OpenAI-compatible endpoint → refused by the fake key → **reported
as unavailable, not dressed up as an answer**. Remove the file and `/health` says
`llm_source: none`, which is the same honesty in the other direction.

So what is missing for Gemini is now only a real key. One line in that file and the model path
is live; the same key is what will give the phone a microphone (Phase 43's `can_listen`).

## 5. Numbers

```
new tests        10
full suite       1856 passed · 0 failed · 0 xfailed · 0 skipped
tools 134 · planner routes 47 · API routes 108/106            (all unchanged)
test modules     72          reports 47
size             62,953 lines of versioned Python/HTML/JS (39,474 source · 23,479 tests)
delivered app    rebuilt, copied to ~/Downloads/Vantage, `codesign --verify` valid after the
                 copy, launched, /health answers -- the key file is not in the bundle, so
                 adding one cannot do the damage Phase 40 found
```

## 6. Carried forward

* **45-A** the phone's microphone button, still waiting on ears (43-A). The key file is now the
  thing that could give it some: hosted Whisper over the same endpoint.
* **45-B** `llm_source` says where, not whether. A real reachability probe is still 42-B, and
  it needs a design that does not make `/health` a bill: cache the result, only on write, or
  show it in the app panel where a human is looking.
* **45-C** the file is plain `KEY=value` with no quoting rules beyond stripping one layer of
  quotes, and no `${VAR}` expansion. Enough for the four variables it exists for.
* **45-D** `apply_llm_file()` runs inside the cached `get_settings()`, so a key added while the
  app is running is picked up on the next launch, not mid-flight. That is the right default for
  a credential, and it should stay a documented choice rather than become a surprise.
* Unchanged: 44-A…D, 43-A…D, 42-B…D, 41-B…F, 40-A…E, 39-A…E, 38-A…D, 37-A…E, 36-A…E,
  35-A…C, 34-A…C.

## 7. Git

Committed on `main`, not pushed. Files: `jarvis/config.py`, `jarvis/api/app.py`,
`tests/test_phase45_a_key_outside_the_bundle.py`, `README.md`, `benchmarks/*`, `assets/*`,
this report.
