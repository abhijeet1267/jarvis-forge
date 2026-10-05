# Phase 64 — the API's shape is not for passersby

Date: 2026-10-05. Repo: `jarvis-forge/`, HEAD at start `850b4b1`.
Trigger: the "check everything is working" sweep found one defect that belonged to this agent
rather than to the user. It had already been found once before, by Phase 9, and had never been
closed on the host.

---

## 1. Measured before any code changed

Against the process the user is actually running (`Vantage.app`, pid 25197, bound to
`0.0.0.0:8765`, `~/.jarvis-forge/lan_mode` = `true`):

| Request | Result |
| --- | --- |
| `GET http://127.0.0.1:8765/openapi.json` | **200**, 82,556 bytes |
| `GET http://172.20.168.82:8765/openapi.json` | **200**, 82,556 bytes |
| `GET http://172.20.168.82:8765/docs` | **200**, 1,011 bytes |
| `GET http://172.20.168.82:8765/redoc` | **200** |
| `GET http://172.20.168.82:8765/health` | 401 |
| `GET http://172.20.168.82:8765/tools` | 401 |
| `GET http://172.20.168.82:8765/` | 200, 93 bytes, body carries `"docs": "/docs"` |

`172.20.168.82` is this laptop's own Wi-Fi address, so the request needed no spoofed header and
no token. `host_allowed` accepts it because `lan_mode` is on — that is precisely what lets a phone
in the same room reach the API (Phase 35), and it is also what makes the documentation readable
from that room. The credential gate held on every route this project wrote; the only thing that
answered for free was the description of everything those routes can be asked to do.

The same probe re-run immediately before the redeploy gave identical bytes, which is the number
carried into this report.

### The finding is nine phases old

`audit/PHASE9_FINAL_REPORT.md` measured `GET /openapi.json` → **200 unauthenticated, 71,047
bytes, 96 paths including `/command`, `/confirm/{id}`, `/privacy/delete`, `/memory/export`** and
filed it **P2**. `BASELINE_REPORT.md:176` lists the same unauthenticated set. Phase 59 closed
exactly these three paths **on the relay**, with a test named for it
(`test_the_relay_answers_only_for_the_endpoints_it_defines`). The host was never closed, because
after Phase 9 nobody re-listed the host's own route objects — and each phase measured the routes
it had added, all of which correctly require a token.

That is the shape of the miss: the defect was not unknown, it was *inherited as a known fact
about a different component*.

---

## 2. The change

Two files, 39 lines added.

- **`jarvis/config.py`** — `api_docs: bool = False` in the API-security block. `env_prefix`
  makes it `JARVIS_API_DOCS=1`, and the field carries the reason in its own comment because a
  setting that reads like a convenience is the kind that gets turned on by someone who did not
  measure it.
- **`jarvis/api/app.py`** — `doc_routes(settings)` returns
  `{"docs_url": None, "redoc_url": None, "openapi_url": None}` unless asked otherwise, and is
  splatted into `FastAPI(...)`. The decision is at construction because that is when FastAPI
  reads these names; there is no per-request check to write.
- **`root()`** now adds `"docs": "/docs"` only when the page exists. The key is **absent** rather
  than null: an index that reports a closed door is still describing a door.
- **Startup** prints `API docs -> /openapi.json, unauthenticated, on every address listed above`
  when the switch is on, next to the lines that already name the phone URLs. An open door should
  be visible from outside the room.

Nothing else changed: no tool, no route handler, no permission, no gate. `JARVIS_API_DOCS=1`
opens the host and **not** the relay — `build_relay_app` hardcodes the off-values, which stays the
narrower rule for the component whose position is outside the house.

---

## 3. Tests — `tests/test_phase64_the_api_shape_is_not_for_passersby.py`, 12

Red first: the file was written before `doc_routes` existed and failed collection with
`ImportError: cannot import name 'doc_routes'`.

| Test | What it refuses to let happen |
| --- | --- |
| `..._mounts_no_route_that_publishes_its_own_shape` | Asserts on **route objects**, not status codes — a 404 can also come from a handler that exists. |
| `..._request_for_the_blueprints_is_a_404` | The four paths, anonymously. |
| `..._404_is_a_refusal_and_not_a_dead_app` | `/` → 200 and `/health` → 401 in the same test, so "everything 404s" cannot pass by the app failing to start. |
| `..._index_does_not_advertise_a_page_it_does_not_serve` | The public index pointing at `/docs`. |
| `..._shape_stays_closed_to_a_credential_that_could_have_read_it` | A token holder no longer gets the schema either, and still gets 148 tools. |
| `..._switch_is_off_unless_a_person_turns_it_on` | `Settings.api_docs` defaults to `False`. |
| `..._env_var_actually_reaches_the_setting` | A misspelled `env_prefix` would pass the default-value test and fail this one. |
| `..._off_values_are_the_names_fastapi_reads` | Exact keys. A typo'd keyword is not an error to FastAPI — it is a default that still mounts the route. |
| `..._opted_in_host_serves_the_shape_it_declined_by_default` | The switch really opens it. Built as a **separate** app, not by reloading `jarvis.api.app`: reloading would swap the shared `app` for every later test — Phase 33's stub handlers, Phase 63's shared OCR backend. |
| `..._fastapi_app_built_without_the_helper_still_leaks_its_shape` | **Negative control.** Without it, `doc_routes` returning `{}` and returning the off-values look identical. |
| `..._relay_and_the_host_decline_the_same_three_paths` | Phase 59's rule and Phase 64's, compared as route objects so the two cannot drift apart silently. |
| `..._every_mounted_route_is_one_this_project_wrote` | The class, not the list: `app.routes` minus the paths written in `app.py` is empty. Its own control is that a bare `FastAPI()` returns exactly the three blueprints. |

One of my assertions was wrong and the code was right: I wrote `tools["tools"]` for `/tools`,
which returns a **list** of specs (`async def tools(...) -> List[Dict[str, Any]]`), and the test
failed with `TypeError` on the first run. Fixed in the test, and that test now also pins the
token-holder's 404.

**Focused:** `126 passed` across this file plus `test_api.py`,
`test_phase10_token_disclosure.py` (both of which fetch `/openapi.json` in the course of proving
it does not leak the master token — a 404 body leaks nothing, so they stayed green without
edits) and the Phase 59 relay file.

**Full suite:** `2408 passed, 0 failed` in 142 s — 2,396 before, plus the 12 here.

---

## 4. Live proof, on isolated homes

`JARVIS_HOME=$(mktemp -d)`, loopback bind, ports 8914/8915 — the user's real home was not written
to and its running process was not touched.

**Default (`python run.py serve`)**

```
/openapi.json            404  22B
/docs                    404  22B
/redoc                   404  22B
/docs/oauth2-redirect    404  22B
/health                  401  42B        (anonymous)
/                        200  78B  {"name":"Vantage Core","version":"0.1.0","dashboard":"/dashboard","tools":148}
```

With a token: `/health` 200, `tools 148`, `ai_provider openai`. The startup log contains **no**
`API docs` line, which is the same fact as the 404s expressed from the other end.

**Opt-in (`JARVIS_API_DOCS=1`)**

```
API docs               -> /openapi.json, unauthenticated, on every address listed above   (printed at start)
/openapi.json    200  82556B
/docs            200  1011B
/                200  93B   ...,"docs":"/docs"
/health          401  42B
```

82,556 bytes is byte-for-byte the size the delivered host was serving before the change, so the
opt-in is the old behaviour and not a new document. `/health` stays 401 in both modes.

Both scratch servers stopped, both temp homes deleted. The delivered app was re-checked afterwards
with one authenticated `GET /health` → 200; its process is `launcher.py`, which is why the
`run.py serve` cleanup did not reach it.

---

## 5. Numbers, re-measured

| Measure | Before | After |
| --- | --- | --- |
| Tests passing (full suite) | 2,396 | **2,408** |
| Test modules | 91 | **92** |
| Mounted route objects | 122 | **118** |
| Distinct mounted paths | 115 | **111** |
| `@app.*` decorators in `jarvis/api/app.py` | 118 | **118** |
| Tools / confirmation-gated | 148 / 32 | 148 / 32 |
| Risk split (read/low/external/destructive/physical) | 78/38/15/14/3 | unchanged |
| Planner rules · DB tables | 52 · 32 | 52 · 32 (tables re-counted on a fresh home: 32) |
| Versioned lines (`git ls-files -co … \| xargs grep -c ''`) | 75,515 | **76,123** |
| — shipped (`jarvis/ dashboard/ app/ assets/ run.py`) | 44,238 | **44,271** |
| — tests | 30,659 | **31,231** |
| Audit reports | 65 | **66** |

The route arithmetic is the check that this phase removed nothing it authored: decorators
(source-written routes) stayed at 118 while mounted objects fell 122 → 118 and paths 115 → 111 —
exactly the four FastAPI supplies on its own.

---

## 6. Not done here, and whose it is

1. **The 5 `forged` rows in this machine's real ledger.** Measured on the live host through
   `/metrics`: `chain_intact: false`, `chain_checked: 495` of `sampled: 500`, and
   `by_result.forged: 5` (denied 16, blocked 65, awaiting_confirmation 69 sit beside it). The
   verifier is right and the arithmetic is right — 500 walked, 495 proved: `tests/test_security.py` deliberately
   `UPDATE`s rows to `result='forged'` to prove tamper detection, and those writes landed in the
   default home before `tests/conftest.py` gave each session its own. Making the chain read green
   means deleting real audit history, which this project's rules forbid an agent from doing to
   improve a metric. **User's decision.**
2. **9 leftover `phone_*` pairing rows, 6 of them still `paired`.** Read-only from `/devices` on
   the live host: `phone_main`, `phone_pair_a`, `phone_twice`, `phone_perm`, `phone_tool`,
   `phone_hmac` are `paired`; `phone_remove`, `phone_temp`, `phone_gone` are `revoked`; all dated
   **2026-09-25**, none from this session. Six held pairing secrets for handsets that do not
   exist, which is a state worth closing — but revoking is reversible only by re-pairing, and
   deleting a row is not reversible at all, and one of those ids could be the user's real phone
   from a month ago. **User's decision**, with the exact list above.
3. **Not measurable on this machine:** whether anyone ever read those pages. No access log records
   `GET /docs`, and no evidence was sought. What this phase claims is *reachability*, which was
   measured from this laptop's own LAN address; it does not claim prior exfiltration, and nothing
   in the 82,556 bytes was a credential.
4. **64-A, left open deliberately:** the four closed routes are named in tests, and the
   class-level guard now catches a future one, but the underlying mechanism — a route FastAPI adds
   before any dependency is attached — remains available to the next version of the library.
   **64-B:** `JARVIS_API_DOCS` is read at import, so toggling it needs a restart. Stated rather
   than engineered: a per-request gate would mean middleware for a page that should not exist by
   default.
