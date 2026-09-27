# PHASE 9 — MEMORY INTEGRITY AUDIT (execution-verified)

Scope: `jarvis/memory/`, `jarvis/tools/memory_tools.py`, `jarvis/tools/memory_controls.py`,
`jarvis/security/privacy.py`, `jarvis/security/audit.py`, memory/privacy routes in
`jarvis/api/app.py`, and every derived store the pipeline touches.

Mode: **read-only on production code.** No file in `jarvis/`, `app/`, `tests/` was modified.
The only repository write is this file.

Central property under attack: **information that was deleted/forgotten must never come back.**

---

## 0. How this was executed (reproducibility)

* Every experiment ran with `JARVIS_HOME=$(mktemp -d)`. The user's real `~/.jarvis-forge`
  was never opened for writing.
* Tool success-paths are dead in production (`jarvis/tools/base.py:16` does not import
  `Verification`, used at `:103/:110/:117`). To exercise them a throwaway copy was made:

  ```bash
  rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude dist \
    /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/ /tmp/p7shadow/
  # /tmp/p7shadow/jarvis/tools/base.py:16 -> from ..core.models import ActionResult, Verification
  ```

  That single line is the only difference between the shadow copy and the repo.
* Runner: `/Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python` (3.11.16).
* Scratch scripts (in `/tmp/p7scratch/`, deliberately outside the repo):
  `exp01_lifecycle.py`, `exp02_disk_recovery.py`, `exp03_pipeline.py`,
  `exp04_scope_ttl_controls.py`, `probe_p0.py`, `exp05_privacy_export.py`,
  `exp06_graph_rrf_limits.py`, `exp07_window_wal_export.py`, `exp08_leaktable.py`,
  `exp09_network_boundary.py`, `exp10_gating_confirmations.py`.
* Verdict vocabulary: **PASS** (property held), **FAIL** (property broken),
  **UNVERIFIED** (could not execute; stated as code-evidence only).

---

## 1. Subsystem inventory (file:line)

| Subsystem | Where it actually lives | Notes found while reading |
|---|---|---|
| Short-term memory | `jarvis/core/models.py:75` `MemoryScope.TEMPORARY/SESSION`; rows in `memories` (`jarvis/db/database.py:274`) | No lifetime code at all; scope is only a column. `purge_expired_memories` (`jarvis/security/privacy.py:295`) has **zero callers** (grep: only the definition). |
| Long-term memory | same table, `scope='long_term'`; write path `jarvis/memory/store.py:74` `remember()` | Append-only. Only `UPDATE memories` statements in the codebase touch `embedding` (`jarvis/memory/vector_store.py:54,76,132`). |
| Project memory | `scope='project'` + `project_id` (`store.py:81,112`); set from `remember_fact` (`jarvis/tools/memory_tools.py:30-49`) and `POST /memory` (`jarvis/api/app.py:415-429`) | `project_id` is stored but **never used as a filter in retrieval** (`store.py:182` `search()` has no scope/project/privacy predicate). |
| Keyword retrieval (BM25-lite) | `jarvis/memory/store.py:182-250`; tokenizer/stopwords/stemmer `store.py:24-64` | Window: `SELECT * FROM memories ORDER BY created_at DESC LIMIT 1000` (`store.py:197`). |
| Vector retrieval | `jarvis/memory/vector_store.py:37` `VectorIndex`; embedders `jarvis/memory/embeddings.py:123` (hashing, default) / `:157` (remote) | Vectors are a column on the same row (`memories.embedding`), so there is **no on-disk index file** (confirmed by directory inventory, EXP-07/§2.11). Window: `LIMIT 5000`, no `ORDER BY` (`vector_store.py:80-83`). |
| RRF fusion | `jarvis/memory/hybrid.py:65` `fuse()`, `:89` `hybrid_search()`, `RRF_K=60` (`:39`) | Degrades to `keyword_only` when the vector side returns nothing (`:124-129`). |
| Retrieval surfaces (tools) | `search_memory` `jarvis/tools/memory_tools.py:60`; `memory_overview` `:63`, `inspect_memory` `:107`, `export_memories` `:141`, `forget_memory` `:198`, `clear_memory_scope` `:218`, `sweep_expired_memories` `:250`, `set_memory_category` `:268` (all `jarvis/tools/memory_controls.py`) | |
| Retrieval surfaces (HTTP) | `GET /memory` `app.py:406`, `POST /memory` `:415`, `GET /memory/overview` `:1091`, `GET /memory/search` `:1117`, `GET /memory/export` `:1132`, `GET /memory/{id}` `:1166`, `POST /memory/{id}/forget` `:1188`, `POST /memory/clear` `:1209`, `POST /memory/reindex` `:1234`; startup vector backfill `app.py:83-95` | |
| Project graph | `jarvis/memory/graph.py:34` `ProjectGraph` — `create_project:41`, `add_node:65`, `link:96`, `nodes:107`, `neighbors:116`, `summarize:127`, `explain_failures:134` | **No delete/remove API exists on `ProjectGraph` at all.** `graph_edges` has no `FOREIGN KEY` clause (`db/database.py:306-312`) so no cascade is possible. |
| Deletion | `store.py:154` `forget()` — single statement: `DELETE FROM memories WHERE id = ?` | Does not touch `graph_nodes`, `graph_edges`, `messages`, `action_logs`, `failures`, `tasks`, `audit.jsonl`, or other memories' `relationships`. `VectorIndex.forget()` (`vector_store.py:74`) exists but is **never called**. |
| Scoped deletion | `memory_controls.py:238` and `app.py:1220` — `DELETE FROM memories WHERE scope = ?`; bulk scopes `privacy.py:168` `DELETE_SCOPES` → `privacy.py:251` `delete()` | `DELETE_SCOPES` never lists `sessions`, and never touches `audit.jsonl` or export files. |
| Sensitive-memory protection | `memory_controls.py:31` `SENSITIVE_LEVELS = {private, secret}`; used only in `export_memories:165` and duplicated inline in `app.py:1147` | Filter keys on the **row's own** `privacy_level`, on two export paths only. No filter on list/search/graph. |
| Redaction / privacy mode | `jarvis/security/privacy.py:81` `redact()`, `:119` `redact_value()`, `:36-59` patterns; toggle `config.py:64-70`; route `app.py:670` | Applied in `AuditLog.record` (`security/audit.py:75-79`) and `PrivacyManager.export` (`privacy.py:207`). **Not** applied in memory export or in the embedder. |
| Export | `PrivacyManager.export:188` / `write_export:220` (redacted); `export_memories` tool `:141`; `GET /memory/export` `app.py:1132` (neither redacts) | |
| Expiry / TTL | `models.py:362` `is_expired()`; tool `sweep_expired_memories` `memory_controls.py:250`; `PrivacyManager.purge_expired_memories:295` | Nothing filters expired rows at read time; no scheduler hook (`jarvis/scheduler/*` and `run.py` contain no sweep/purge reference — verified by grep). |
| Confidence scores | column `memories.confidence` (`database.py:281`), default `1.0` (`store.py:82`), used as a rank multiplier (`store.py:235`) | Write-once: no code path ever updates, decays, or demotes it. |
| Relationships | `models.py:360`, column added in `_migrate` (`database.py:422`), written `store.py:131`, surfaced `memory_controls.py:133`, `app.py:1185` | Free-form `{rel: [ids]}`; no integrity check, no dangling-ref pruning. |
| Derived copies (the actual risk surface) | `orchestrator.py:451-460` `_remember_outcome()` → **new memory row containing the raw request + result text**; `orchestrator.py:107-118` `_log_message` → `messages.content`; `orchestrator.py:120-146` `_audit_action` → `action_logs.intent = request.text`, `target = json.dumps(params)`, `detail.message`; `core/tasks.py:25-40` → `tasks.goal`; `core/recovery.py:228-253` → `failures.params/error/note`; `security/audit.py:234-240` → `audit.jsonl`; `security/policy.py:247` → `confirmations.params/preview` | None of these are reachable from `MemoryStore.forget`. |

---

## 2. Experiments

### EXP-01 — remember → retrieve → delete → retrieve again (library layer)
`python /tmp/p7scratch/exp01_lifecycle.py` (shadow copy)

Setup: one `long_term` canary memory (`the deployment passphrase rotation for CANARYZXQ4471 …`),
one *related* memory referencing its id, one graph node whose payload embeds the same sentence,
one graph edge.

```
stored memory id = MEM-0e465a4496c7   related = MEM-46aff1658e74   node = NODE-686a8e9b7ab9
=== BEFORE DELETE ===
  store.get(id): HIT
  MemoryStore.search (keyword): ['MEM-0e465a4496c7']
  VectorIndex.search (vector): [('MEM-0e465a4496c7', 0.356)]
  hybrid_search mode: hybrid
  hybrid_search contents: ['the deployment passphrase rotation for CANARYZXQ4471 happens on the blue cluster']
  SQL tables/columns containing canary: {'memories.content': 1, 'graph_nodes.label': 1, 'graph_nodes.data': 1}
  ON-DISK byte occurrences of canary: {'jarvis.db-wal': 6}

store.forget(MEM-0e465a4496c7) returned True
dangling relationships pointing at deleted id: [{"id": "MEM-46aff1658e74", "rel": {"depends_on": ["MEM-0e465a4496c7"]}}]

=== AFTER DELETE ===
  store.get(id): gone
  MemoryStore.search (keyword): no hits
  VectorIndex.search (vector): no hits
  hybrid_search mode: keyword_only        hybrid_search contents: []
  SQL tables/columns containing canary: {'graph_nodes.label': 1, 'graph_nodes.data': 1}
  graph_edges rows: 1
  ON-DISK byte occurrences of canary: {'jarvis.db-wal': 7}

VectorIndex.index_all() after delete -> 0 rows embedded
store.forget('MEM-doesnotexist') -> True
```

Verdict:
* Row-scoped deletion **PASS** for the row itself: keyword index, vector column and RRF fusion all
  stop returning it, because the vector lives in the same row (`vector_store.py:1-9`). Re-indexing
  cannot resurrect it.
* **FAIL** for the project graph (node label + `data` payload + edge survive and are user-readable),
  for `relationships` (dangling ids kept), and for on-disk bytes.
* **FAIL** (contract): `forget()` returns `True` for a nonexistent id (`store.py:154-157`).

### EXP-02 — forensic recovery from the database files
`python /tmp/p7scratch/exp02_disk_recovery.py`

```
live rows containing canary BEFORE delete: 1
live rows containing canary AFTER  delete: 0
PRAGMA secure_delete = 0
canary occurrences per file: {'jarvis.db-wal': 3}
page-carve preview (byte offset 207058):
  b'...MEM-cf231cac46f7the house alarm code context for DELELETETHIS-9F2C8A is behind the hall closetuserlong_terminternal[][0.0, -0.067493, ...'
```

`strings -n 8 jarvis.db | grep DELELETETHIS` recovers the deleted row verbatim (see EXP-07B for the
post-checkpoint case). Verdict: **FAIL** — deletion is logical only; the full record *and its embedding
vector* remain byte-recoverable in the WAL.

### EXP-03 — full pipeline: spoken memory, HTTP forget, retrieve again (the headline test)
`python /tmp/p7scratch/exp03_pipeline.py`

Setup: one real command through `JarvisCore.handle()`:
`"remember that my late wife's hospital ward was WARD-7781-ZXQ room 4"`, then
`POST /memory/{id}/forget`, then every retrieval surface.

```
reply: Got it. I will remember: my late wife's hospital ward was WARD-7781-ZXQ room 4
--- where the canary lives in SQL (AFTER REMEMBER) ---
  messages.content: n=2 | action_logs.intent: n=1 | action_logs.target: n=1
  action_logs.detail: n=1 | memories.content: n=2
all memories now in store:
   MEM-f21780eaf912 [session/orchestrator] User asked: remember that my late wife's hospital ward was WARD-7781-ZXQ room 4. Result: remember_fact: Got it
   MEM-9a79dfdb8b12 [long_term/user]       my late wife's hospital ward was WARD-7781-ZXQ room 4

POST /memory/MEM-9a79dfdb8b12/forget -> 200 {'memory_id': 'MEM-9a79dfdb8b12', 'deleted': True}
=== AFTER FORGET ===
store.get(mid)      -> None
GET /memory/{id}    -> 404
GET /memory/search  -> mode hybrid hits [
   "User asked: do you remember WARD-7781-ZXQ?. Result: search_memory: Here is what I remember: my late wife's hospital ward was WARD-7781-ZXQ room 4",
   "User asked: remember that my late wife's hospital ward was WARD-7781-ZXQ room 4. Result: remember_fact: Got it. I will remember: ..."]
keyword search      -> 2 hits (both session/orchestrator echoes)
search_memory tool  -> "Here is what I remember: User asked: do you remember WARD-7781-ZXQ?. Result: ... my late wife's hospital ward was WARD-7781-ZXQ room 4"
GET /memory items containing canary        -> 2 rows
/memory/export entries containing canary   -> 2 rows
--- where the canary lives in SQL (AFTER FORGET) ---
  messages.content: n=4 | action_logs.intent: n=2 | action_logs.target: n=2 | action_logs.detail: n=2 | memories.content: n=2
/privacy/export contains canary: True   (memories=2, messages=4, action_logs=3)
audit.jsonl: 6 occurrence(s) of canary
```

Verdict: **FAIL — the central property is broken.** The API answers `deleted: True`, and the memory
is genuinely gone, yet the *same sentence* is returned by hybrid (RRF) search, by the `search_memory`
tool, by `GET /memory`, and by `GET /memory/export`. Cause: `orchestrator.py:451-460`
`_remember_outcome()` writes a `session`-scoped memory quoting the whole exchange, so the fact has a
second live copy *inside the memory subsystem itself*. It is also **self-amplifying**: the recall
attempt after deletion created a *third* echo that quotes the earlier echo.

### EXP-04 — scoped deletion, TTL, category controls, and the shipped-repo tool path
`python /tmp/p7scratch/exp04_scope_ttl_controls.py`

```
### 4A clear_memory_scope('session')
created one memory per scope; after clearing `session`:
surviving after clearing `session`: {'temporary': True, 'session': False, 'project': True, 'long_term': True, 'private': True}
memories rows still holding embeddings: 4
search() ignores scope -- hits scopes: ['long_term', 'session', 'project', 'long_term', 'private']
```
Scoped deletion **PASS** (only the targeted scope is removed; the deleted row's vector goes with it).
Retrieval isolation **FAIL** — a `private`-scope memory answers an unscoped query.

```
### 4C TTL
expired item is_expired(): True
keyword search returns expired memory: [True, False]
hybrid_search returns expired: [(True, 'temporary'), ...]
GET-style list all returns expired: True
manual sweep -> "Removed 1 expired memory item(s)." | gone: True
purge_expired_memories / sweep wired to scheduler? NO CALLERS
```
Verdict: **FAIL** — expiry is advisory. Expired memories stay fully retrievable until a human invokes
the sweep; nothing in `jarvis/scheduler/` or `run.py` ever calls it.

```
### 4D naive expires_at
remember() ok, stored expires_at: 2020-01-01 00:00:00
remember/is_expired raised: TypeError can't compare offset-naive and offset-aware datetimes
sweep_expired_memories RAISES: TypeError can't compare offset-naive and offset-aware datetimes
```
Verdict: **FAIL (latent)** — one naive-tz `expires_at` permanently breaks the sweep and
`memory_overview` (which calls `is_expired()` at `memory_controls.py:82`). Reachability caveat: no
in-repo caller passes `expires_at` today, so this is a public-API trap, not a live user path.

```
### 4E disabling a category
set_memory_category(fact, False) -> "Disabled the fact category. New memories of that kind will not be stored."
category_enabled('fact') = False
rows before=8 after=9  new id=MEM-95e77be886a6 type=fact
=> store.py references to category_enabled: NOT REFERENCED -> not enforced
```
Verdict: **FAIL** — the control is decorative. `category_enabled()` (`memory_controls.py:37`) has no
caller in any write path; `remember()` stores anyway and the tool claims otherwise.

```
### 4B delete('everything')
delete('everything') -> {"memories": 11, "graph_nodes": 1, "graph_edges": 1, "projects": 1, ...}
search for erased fact: []
byte occurrences per file after delete-everything: {'jarvis.db-wal': 11}
```

```
### 4F shipped (unpatched) repo: do the delete tools even run?
$ JARVIS_HOME=$(mktemp -d) ./.venv/bin/python /tmp/p7scratch/probe_p0.py   # cwd = real repo
memory_overview RAISES NameError name 'Verification' is not defined
export_memories  RAISES NameError name 'Verification' is not defined
forget_memory    RAISES NameError name 'Verification' is not defined
```
Verdict: in production the *conversational* memory controls (`forget_memory`, `export_memories`,
`memory_overview`, `sweep_expired_memories`, `set_memory_category`, `clear_memory_scope`) cannot
return a success result at all — the known `base.py:16` P0 hits every `ok()` call. Only the HTTP
routes (`/memory/{id}/forget`, `/memory/clear`) work, because they return plain dicts. This is the
established P0, reported here only for its memory-subsystem impact: **the user's delete/export
controls are unreachable by voice/CLI.**

### EXP-05 — privacy and export boundaries
`python /tmp/p7scratch/exp05_privacy_export.py`

```
### 5C privacy level actually used for a spoken memory
spoken fact stored with privacy=internal scope=long_term

### 5A does the sensitive filter actually withhold?
GET /memory/export        withheld = 2 | contains PRIVFACT-5511: True     <-- BYPASS
GET /memory (no filter)   PRIVFACT rows = ['private', 'internal', 'internal']
GET /memory/search        returns privacy levels = ['my therapist notes say PRIVFACT-5511 self-harm risk is low', ...]
/privacy/export           private rows included = ['internal', 'internal', 'private'] | privacy filtering applied: False
export_memories tool      withheld = 2

### 5B redaction in the two export paths
GET /memory/export carries the raw api_key: True
   sample: bank credential api_key: sk-live-abcdef1234567890
/privacy/export carries the raw api_key: False
   /privacy/export version: bank credential api_key: [redacted]
   personal fact present in /privacy/export action_logs: True

### 5E after 'delete everything', what survives on disk?
audit.jsonl exists: True occurrences of PRIVFACT-5511: 3
delete('everything') -> {"memories": 4, "messages": 2, "action_logs": 1, "graph_nodes": 1, "projects": 1}
live rows: {..., 'sessions': 1, ...}          <-- sessions table untouched
audit.jsonl still on disk: True size: 718 PRIVFACT occurrences: 3
files still holding the erased plaintext: {'jarvis.db-wal': 37, 'audit.jsonl': 3}
```

Verdicts:
* Sensitive-memory protection **FAIL** — the "withheld" export still contains the private *content*,
  because the orchestrator's echo row carries its own `privacy_level = internal` while quoting the
  private fact verbatim. A per-row privacy flag cannot survive a plaintext copy.
* Export redaction **FAIL / inconsistent** — `GET /memory/export` and the `export_memories` tool never
  call `redact()`; `/privacy/export` does. Same data, two guarantees. The README (§"User controls")
  states exports withhold sensitive memories; it does not say one of the two exports leaks
  credentials.
* Graph boundary **FAIL (structural)** — `graph_nodes` has no privacy column at all
  (`['node_id','project_id','kind','label','data','created_at']`), so anything copied into a node's
  `data`/`label` loses its classification permanently.
* Delete-everything **PARTIAL** — leaves `sessions` rows and the whole `audit.jsonl`.

### EXP-06 — graph integrity, relationships, RRF sanity, retrieval windows
`python /tmp/p7scratch/exp06_graph_rrf_limits.py`

```
### 6A graph survives memory deletion
after store.forget(memory):
  memories rows: 0 | graph_nodes rows: 2 | graph_edges rows: 2
  -> edge to deleted memory survives: True
  node payload still holding the deleted sentence: [{'memory_id': 'MEM-e42d11bd8ae2', 'label': 'gearbox overheated GRAPHDEL-31'}]
  explain_failures -> [{"failure": "gearbox overheated GRAPHDEL-31", "detail": {"memory_id": "MEM-e42d11bd8ae2"}, ...}]
  FK on graph_edges?: [] (empty = no cascade)
```
Verdict **FAIL**: deleted content resurfaces through a *live* user-facing read
(`explain_failures` / `project_summary` / `GET /projects`), and the edge to the dead memory id
persists forever because `graph.py` exposes no delete method.

```
### 6B cross-project leakage via neighbors()
  neighbors(project-A failure) = [{'kind':'FIX','label':'added fan'}, {'kind':'FAILURE','label':'private clinical detail CROSSPROJ-77'}]
  explain_failures(project A) leaks project B label: True
### 6C relationships after deletion
  survivor relationships: {'depends_on': ['MEM-d1a0a68e9db7'], 'explains': ['MEM-d1a0a68e9db7', 'MEM-deadbeef0000']}
  dangling id present in DB: 1
### 6D RRF fusion sanity
  fuse() -> {"m1": 0.01639…, "m2": 0.03252… (kw 2, vec 1), "m3": 0.03174…, "m9": 0.01612…}
  m2 (both lists) score > m1 (keyword only)? True
  duplicate ids in one list double-count? -> {"d": {"score": 0.0325…, "keyword_rank": 2}}
  hybrid on a no-overlap query: {"mode": "hybrid", "keyword_candidates": 0, "vector_candidates": 1}
```
RRF math **PASS** — fusion is rank-based, agrees across scales, cross-list winners rank first, and a
zero-overlap query is still answered by the vector side (that is exactly the path that carried the
deleted sentence back in EXP-03). Minor defect: a duplicated id in one list is added twice and its
recorded rank is overwritten.

```
### 6E retrieval window limits  (1003 extra rows written)
  keyword search finds the oldest needle: NOT FOUND
  vector search finds it: [('MEM-017d633226e7', 0.485)]
### 6F confidence is write-once; no correction path
  UPDATE statements touching memories: vector_store.py:54 / :76 / :132   (embedding only)
  both stale and corrected facts retrievable: ['the api base url is OLD-URL-aaa', 'correction: the api base url is NEW-URL-bbb']
  stale confidence still 1.0
  MemoryStore public methods: ['all','db','forget','get','remember','search','stats']
```
Verdicts: **FAIL** — once the collection passes ~1000 rows the keyword side silently stops seeing
older memories (`store.py:197`), so a memory is findable by vector but not by BM25, and degrades to
"invisible" whenever the vector side is unavailable. **FAIL** — there is no update/supersede API, so a
corrected fact cannot retire the stale one; both stay retrievable at confidence 1.0.

### EXP-07 — vector window, WAL checkpoint, orphan export files
`python /tmp/p7scratch/exp07_window_wal_export.py`

```
### 7A vector window (LIMIT 5000, no ORDER BY)
  inserted+embedded 5004 rows in 0.6s | rows with embedding: 5004
  _load() returned 5000 vectors; newest id among them: False | oldest sample: MEM-000000000000
  vector search for the newest memory's exact text -> ['MEM-000000002637', ... 20 old ids, newest absent]
  keyword search for the same text -> []
### 7B where deleted plaintext lives after a WAL checkpoint
  before checkpoint: {'jarvis.db-wal': 5}
  checkpoint: [{'busy': 0, 'log': 0, 'checkpointed': 0}]
  after  checkpoint: {'jarvis.db': 2}
  live SQL view: 0
  recoverable by `strings jarvis.db`: ['MEM-52c99cd2c415WALCANARY-6620 top secretuserlong_terminternal[]2026-09-25T15:02:54.289782']
### 7C export files are outside every deletion scope
  wrote export_20260925_203254.json mode 0o600 contains canary: True
  delete('everything') -> 5005 rows
  export file still on disk: True  still contains canary: True
```
Verdicts: **FAIL** — newest memories are unreachable by the vector retriever past 5000 embedded rows
(a scan-order artifact, not a ranking choice). **FAIL** — after a WAL checkpoint the deleted row's
plaintext (id, content, source, scope, privacy) is recoverable from `jarvis.db` with `strings`, while
SQL reports 0 rows. **FAIL** — `write_export()` files are not in any `DELETE_SCOPES`, so "delete
everything" cannot un-export.

### EXP-08 — the definitive leak table for ONE deleted memory
`python /tmp/p7scratch/exp08_leaktable.py`

```
--- BEFORE FORGET: DB locations holding the canary ---
   action_logs.intent = 2   action_logs.target = 1   action_logs.detail = 2
   failures.params = 1      failures.error = 1       failures.note = 1
   graph_nodes.label = 1    memories.content = 2     messages.content = 2     tasks.goal = 1
   on-disk files: [('audit.jsonl', 5), ('jarvis.db-wal', 24)]

store.forget -> True

--- AFTER FORGET: DB locations holding the canary ---
   action_logs.intent = 2   action_logs.target = 1   action_logs.detail = 2
   failures.params = 1      failures.error = 1       failures.note = 1
   graph_nodes.label = 1    memories.content = 1     messages.content = 2     tasks.goal = 1
   on-disk files: [('audit.jsonl', 5), ('jarvis.db-wal', 27)]

--- can it still be retrieved by the assistant? ---
search_memory reply: Here is what I remember: User asked: remember that my address is ONELASTFACT-3377 street.
                     Result: remember_fact: Got it. I will remember: my address is ONELASTFACT-3377 street
keyword hits: [('MEM-c0a97e2f72b2', 'session', 'User asked: do you remember ONELASTFACT-3377? …'),
               ('MEM-4c1fab71ca52', 'session', 'User asked: remember that my address is ONELASTFACT-3377 street …')]
rows still in `memories` with the canary: 2   ids: ['MEM-4c1fab71ca52', 'MEM-c0a97e2f72b2']
```
Verdict: **FAIL.** Of 11 recorded locations, exactly one (`memories`, partially) is cleaned by
deletion — and the surviving `memories` rows are auto-generated by the system, not by the user.

### EXP-09 — network / process boundary for sensitive memories
`python /tmp/p7scratch/exp09_network_boundary.py` (local HTTP capture standing in for
`JARVIS_LLM_BASE_URL`)

```
active embedder = remote | remote available = True
POST bodies captured by the fake endpoint: 1
  -> /v1/embeddings  model=text-embedding-3-small
     contains SECRET-privacy memory plaintext: True
     contains the credential text: True
     input sent: SECRET BANK login api_key: sk-live-zzz999 and my doctor said DIAGNOSIS-88
after one hybrid query, captured bodies = 2
  query input sent: what did my doctor say about DIAGNOSIS-88
--- deletion vs the in-process embedding cache ---
cache keys AFTER delete: ['SECRET BANK login api_key: …', 'what did my doctor say about DIAGNOSIS-88']
plaintext still resident in process: True | module singleton holding it: True | any eviction API: NONE
```
Verdict: **FAIL** — `embeddings.py:173-197` sends raw text with no `redact()` call, and `privacy` /
`secret` classifications are ignored, so a `secret` memory's plaintext (including a credential)
leaves the machine on write *and* on every query, contradicting the invariant stated in
`security/privacy.py:1-15` and `config.py:64-70`. Secondary: the embedder's `_cache` keys are raw
texts and survive deletion for the process lifetime.
Caveat: the endpoint was a local capture server, not a real vendor; the code path is the shipped one.

### EXP-10 — device gating and confirmation records
`python /tmp/p7scratch/exp10_gating_confirmations.py`

```
### 10A scope vs privacy-level mismatch
rows: [('MEM-a279c76c1368', 'private', 'internal'), ('MEM-7e64ca3c787c', 'long_term', 'private')]
clear_memory_scope('private') -> "Cleared 1 memory item(s) from the private scope."
privacy=private row still alive: True | its privacy level: private
### 10B device gating
  search_memory from phone_main  -> BLOCK (search_memory is not permitted on device phone_main)
  search_memory from watch_main  -> BLOCK
  remember_fact from watch_main  -> BLOCK
### 10C confirmation record contents
  confirmations row: {'tool': 'forget_memory', 'params': '{"memory_id": "MEM-abc123"}',
                      'preview': 'forget_memory on laptop_main | memory_id=MEM-abc123', 'state': 'pending'}
  canary present in confirmations: False | messages: 1 | action_logs: 1 | memories: 1
```
Verdicts: device gating **PASS** (memory tools are `laptop_main`-only, `memory_controls.py:296` etc.).
`confirmations` **PASS** (ids only, no content). **FAIL**: `MemoryScope.PRIVATE` and
`PrivacyLevel.PRIVATE` are different axes — "clear my private memory" deletes scope rows and silently
leaves `privacy_level='private'` rows behind.

### 2.11 — on-disk index files: none
```
files in JARVIS_HOME after a realistic run:
    api_token 44 | audit.jsonl 613 | jarvis.db 4096 | jarvis.db-shm 32768 | jarvis.db-wal 1895232 | jarvis.log 0
dirs: []
```
There is no FAISS/ANN sidecar to purge; the entire "vector index" is `memories.embedding`. One command
grew the WAL to ~1.9 MB, which is where deleted content accumulates.

---

## 3. Leak table — where deleted memory data persists

`D` = reachable by a live user-facing surface; `F` = forensic/file recovery only; `—` = removed.

| # | Location | Written by | After `MemoryStore.forget()` | How proven | Severity |
|---|---|---|---|---|---|
| 1 | `memories` row (target) | `store.py:109` | removed | EXP-01/EXP-03 `store.get -> None`, `GET /memory/{id} -> 404` | — |
| 2 | `memories.embedding` (vector) | `store.py:139` → `vector_store.py:53` | removed with the row | EXP-01 `VectorIndex.search: no hits`; `index_all -> 0` after delete | — |
| 3 | **`memories` rows, scope `session`, source `orchestrator` — verbatim echo of the fact** | `orchestrator.py:451-460` | **survives, retrievable (D)** | EXP-03, EXP-08 (`memories.content = 1→2`, search returns the sentence) | **P0** |
| 4 | `graph_nodes.label` / `graph_nodes.data` payload | `graph.py:76-89`, `memory_tools.py:138` | survives, readable via `explain_failures` / `project_summary` / `GET /projects` (D) | EXP-06A, EXP-05D | **P1** |
| 5 | `graph_edges` rows pointing at deleted ids | `graph.py:98-104` (no FK, `database.py:306`) | survives (F/D) | EXP-06A `edge to deleted memory survives: True` | P2 |
| 6 | `memories.relationships` of *other* rows | `store.py:131` | survives as dangling ids, surfaced by `inspect_memory` / `GET /memory/{id}` (D) | EXP-01, EXP-06C | P3 |
| 7 | `messages.content` (user + assistant turns) | `orchestrator.py:107-118` | survives (D via `/privacy/export`; no HTTP list route exists for messages) | EXP-03 n=4 after forget; EXP-08 n=2 | **P1** |
| 8 | `action_logs.intent` = raw command text | `orchestrator.py:120-146` | survives (D) | EXP-03/EXP-08 `action_logs.intent = 2` | **P1** |
| 9 | `action_logs.target` = JSON of tool params (memory content) | same | survives (D) | EXP-08 `target = 1` | **P1** |
| 10 | `action_logs.detail` = assistant message ("Got it. I will remember: …") | same | survives (D) | EXP-08 `detail = 2` | **P1** |
| 11 | `audit.jsonl` (append-only, off-DB) | `security/audit.py:234-240` | survives, and survives `privacy.delete('everything')` (F) | EXP-05E (3 occurrences), EXP-08 (5) | **P1** |
| 12 | `tasks.goal` | `core/tasks.py:25-40` | survives (D via `GET /tasks`, `/privacy/export`) | EXP-08 `tasks.goal = 1` | P2 |
| 13 | `failures.params/error/note` | `core/recovery.py:228-253` | survives (D) | EXP-08 `failures.params = 1` | P2 |
| 14 | `jarvis.db-wal` / `jarvis.db` free pages | SQLite, `secure_delete = 0`, no `VACUUM` anywhere | survives; `strings` recovers id+content+scope+privacy+vector (F) | EXP-02, EXP-07B | **P1** |
| 15 | `export_*.json` written by `PrivacyManager.write_export()` | `privacy.py:220-233` | survives *any* deletion scope (F) | EXP-07C | P2 |
| 16 | `RemoteEmbedder._cache` keys (raw text) | `embeddings.py:168,174-176,196` | survives deletion for process lifetime (in-RAM) | EXP-09 | P4 |
| 17 | Remote embedding endpoint (out of trust boundary) | `embeddings.py:180-190` | already exfiltrated; not a local copy | EXP-09 | **P1** |
| 18 | `sessions` table | `orchestrator.py:113-116` | survives `delete('everything')` (not in `DELETE_SCOPES`) | EXP-05E `sessions: 1` | P3 |
| 19 | `confirmations.params/preview` | `security/policy.py:247` | **clean** — stores memory ids, not content | EXP-10C | PASS |
| 20 | Device surfaces (phone/watch) | `policy.py:122` + `allowed_devices` | **clean** — memory tools blocked | EXP-10B | PASS |

---

## 4. Findings

### P0
1. **Deleted memories come back through live retrieval (the stated invariant is broken).**
   `POST /memory/{id}/forget` answers `{"deleted": true}` while `GET /memory/search`,
   the `search_memory` tool, `GET /memory` and `GET /memory/export` all return the deleted
   sentence, because `jarvis/core/orchestrator.py:451-460` (`_remember_outcome`, called for every
   executed command) writes a `session`-scoped memory quoting the raw request and the tool message.
   The echo re-echoes on each recall (EXP-03, EXP-08). No deletion path — `store.forget`
   (`store.py:154`), `clear_memory_scope` (`memory_controls.py:238`), `POST /memory/clear`
   (`app.py:1220`), `PrivacyManager.delete` (`privacy.py:251`) — purges echoes, and no surface
   distinguishes a user-authored memory from a system echo.
   Fix direction: make `_remember_outcome` store a reference (id + intent) instead of the plaintext,
   or record `provenance_of`/`derived_from` and delete/blank derived rows when their source row is
   forgotten.
2. *(Established, confirmed reachable here, not re-litigated)* `jarvis/tools/base.py:16` missing
   `Verification` import ⇒ `forget_memory`, `export_memories`, `memory_overview`,
   `sweep_expired_memories`, `set_memory_category`, `clear_memory_scope` all raise `NameError` on
   their success path in the shipped tree (EXP-04F). The conversational right-to-be-forgotten is
   therefore not merely incomplete, it is unexecutable; only the HTTP routes work.

### P1
3. **Deleted content is trivially recoverable from disk.** `PRAGMA secure_delete = 0`, no `VACUUM`
   call anywhere, WAL never truncated by the app. After forget + checkpoint, `strings jarvis.db`
   yields `MEM-52c99cd2c415WALCANARY-6620 top secretuserlong_terminternal[]…` while SQL reports 0 rows
   (EXP-02, EXP-07B). Any backup / Time Machine / sync of `<home>` preserves every "forgotten"
   memory, including embeddings.
4. **The project graph is a permanent, undeleteable copy.** `ProjectGraph`
   (`graph.py:34`) exposes no delete method; `graph_edges` has no FK (`database.py:306-312`), so no
   cascade is even possible. A deleted memory's text in a node `label`/`data` keeps surfacing via
   `explain_failures` / `project_summary` / `GET /projects` (EXP-06A), and `graph_nodes` has no
   privacy column, so classification is lost on entry (EXP-05D).
5. **Memory export does not redact.** `export_memories` (`memory_controls.py:141`) and
   `GET /memory/export` (`app.py:1132`) dump `content` unmodified, while `/privacy/export`
   (`privacy.py:188-208`) redacts. Measured: raw `api_key: sk-live-abcdef1234567890` in
   `/memory/export`; `[redacted]` in `/privacy/export` (EXP-05B). Two export paths, contradictory
   guarantees; the weaker one is the path documented in the README.
6. **Sensitive-memory protection is bypassable by design.** The `private|secret` filter
   (`memory_controls.py:31,165`, `app.py:1147`) is per-row, so the internal-privacy echo of a
   private fact passes straight through an export that reported `withheld = 2`
   (EXP-05A: `GET /memory/export … contains PRIVFACT-5511: True`). `GET /memory` and
   `GET /memory/search` have no privacy predicate at all, and `/privacy/export` deliberately
   includes private/secret rows.
7. **`privacy`/`secret` content is sent off-box unredacted when an embedding endpoint is
   configured.** `embeddings.py:180-190` POSTs raw memory text (and the raw user query) with no
   `redact()` call and no privacy check (EXP-09), contradicting `privacy.py:1-15` /
   `config.py:64-70`.
8. **Derived stores retain the plaintext of every forgotten memory**: `messages.content`,
   `action_logs.intent/target/detail`, `audit.jsonl` (EXP-03/EXP-08), and deletion scopes leave
   `audit.jsonl` untouched (`privacy.py:150-175`); `tests/test_privacy.py:206-220` asserts the audit
   log must *not* be deleted with memories — so this is intended behaviour, which means the export
   audit trail is an accepted plaintext archive.
9. **`set_memory_category(..., enabled=False)` is decorative** (EXP-04E): the tool replies "New
   memories of that kind will not be stored" while `remember()` never consults
   `category_enabled()` (`memory_controls.py:37`, zero callers; `store.py` has no reference). A user
   who asks JARVIS to stop storing facts keeps storing them.
10. **`MemoryScope.PRIVATE` vs `PrivacyLevel.PRIVATE` mismatch** (EXP-10A): "clear my private
    memory" clears `scope='private'` rows and leaves `privacy_level='private'` rows untouched, while
    the reply claims the scope was cleared.

### P2
11. **TTL/expiry is advisory only.** Expired memories stay retrievable via `search()`,
    `hybrid_search()` and `store.all()` (EXP-04C) because no read path applies `is_expired()`
    (`models.py:362`); the only enforcers are the manual `sweep_expired_memories` tool
    (`memory_controls.py:250`) and `PrivacyManager.purge_expired_memories` (`privacy.py:295`), which
    has **no callers**, and nothing is wired into `jarvis/scheduler/`.
12. **Retrieval silently loses data past fixed windows.** `store.search` ranks only the newest 1000
    rows (`store.py:197`) — a needle in an older memory returned `NOT FOUND` while the vector side
    still found it (EXP-06E); `VectorIndex._load` takes 5000 rows with no `ORDER BY`
    (`vector_store.py:80-83`), so with 5004 embedded rows `_load()` returned exactly 5000 and the
    newest memory was unreachable by semantic search (EXP-07A). Both truncations are unlogged and
    contradict `stats()`'s `coverage: 1.0`.
13. **No scoping in retrieval.** `search()`/`all()`/hybrid ignore `scope`, `project_id` and
    `privacy_level` (EXP-04A returned a `private`-scope row for a generic query; EXP-06B
    `neighbors()`/`explain_failures()` leaked another project's node label). Cross-project and
    cross-scope isolation therefore depends entirely on the caller.
14. **`delete('everything')` is not everything**: `sessions` is missing from `USER_DATA_TABLES` /
    `DELETE_SCOPES` (`privacy.py:150-175`, EXP-05E), `audit.jsonl` is never truncated, and
    `write_export()` files survive every scope (EXP-07C).
15. **No correction/supersede path.** `MemoryStore` exposes only
    `all/forget/get/remember/search/stats`; the only `UPDATE memories` statements touch `embedding`
    (EXP-06F). A corrected fact coexists with the stale one at `confidence=1.0`, so "wrong memory"
    can only be fixed by the P0-broken deletion path.

### P3
16. `MemoryStore.forget()` returns `True` unconditionally, including for unknown ids
    (`store.py:154-157`, EXP-01) — the storage contract cannot distinguish "forgotten" from
    "never existed"; only the tool/route pre-checks (`memory_controls.py:204`, `app.py:1194`) hide
    this.
17. Naive-tz `expires_at` raises `TypeError` in `is_expired()` and permanently breaks
    `sweep_expired_memories` and `memory_overview` (EXP-04D). Latent: no in-repo caller passes
    `expires_at`; `POST /memory` (`MemoryBody`, `app.py:214`) has no field for it.
18. Privacy levels are unreachable for a real user: the planner never passes `privacy`
    (`core/planner.py:417-435`) and `MemoryBody` has no privacy field, so every spoken or HTTP memory
    is `internal` and export-eligible (EXP-05C). `remember_fact` accepts `privacy`
    (`memory_tools.py:24`) but nothing in the pipeline supplies it.
19. Dangling `relationships` ids are kept and re-served (EXP-01, EXP-06C) and
    `graph_edges` retains edges to deleted memory ids (EXP-06A) — no integrity or pruning pass exists.
20. `VectorIndex.forget()` (`vector_store.py:74-76`) is dead code — never called by any deletion path
    (currently harmless because vectors are row-local; it becomes a leak the moment an external index
    is introduced, cf. `database.py:1-8` intent).

### P4
21. `RemoteEmbedder._cache` keys are raw memory/query texts with no eviction API, retained by the
    module-level `_embedder` singleton after deletion (EXP-09).
22. `fuse()` double-counts a duplicated id within one list and overwrites its rank
    (EXP-06D) — affects ranking, not confidentiality.
23. Test-coverage gap: the suite asserts only row-level deletion
    (`tests/test_memory_advanced.py` `test_forget_deletes_one_memory`,
    `test_clear_scope_removes_only_that_scope`) and category *toggling*, never cross-surface
    reachability, on-disk recovery, export redaction parity, or window truncation. Nothing failed
    while every leak above reproduced.

---

## 5. What did hold up

* Vector/keyword/RRF mechanics are correct and honest about mode
  (`hybrid.py:124-152`, `describe_retrieval:166`); fusion is rank-based as documented and the
  keyword-only fallback works (EXP-06D).
* Because embeddings are stored in the memory row, single-memory deletion does remove the vector
  from every live index — no stale-vector ghost in `VectorIndex.search` (EXP-01), and `index_all()`
  cannot re-materialise a deleted row (EXP-01).
* Scoped deletion of `memories` by scope is precise: other scopes survive (EXP-04A).
* Device gating keeps memory tools on `laptop_main` only, and confirmation records store ids, not
  content (EXP-10B/10C).
* `write_export()` is owner-only (`0600`, `privacy.py:228-231`) and `/privacy/export` redacts
  credentials by default (EXP-05B).

## 6. Not verified

* Real vendor embedding endpoint (EXP-09 used a local capture server on `127.0.0.1`); the shipped
  code path was exercised, the remote retention behaviour is inferred from the request body only.
* SQLite forensic recovery *through* a supported API (e.g. `sqlite3 .recover`, or WAL replay after a
  crash): recovery here was demonstrated with `strings`/byte-carving on `jarvis.db` and
  `jarvis.db-wal` (EXP-02, EXP-07B), which is the conservative claim.
* Behaviour under >5000 memories **with concurrent vector search latency** — only the correctness
  window (EXP-07A) was measured, not performance.
* Multi-process/concurrent-writer semantics of the single shared SQLite connection
  (`database.py:370-391`) — out of scope for this phase and not exercised.
* The user's real `~/.jarvis-forge` database was intentionally never opened. Whether existing live
  data already contains echo-derived copies is therefore **UNVERIFIED** by design; given
  `orchestrator.py:451-460` runs on every command with at least one action, expect it to.
