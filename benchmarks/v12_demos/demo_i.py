"""Demo I -- Skill Evolution: an outdated skill fails, a narrowing proposal is
made, it is accepted, and the new version is run and verified."""
from __future__ import annotations

import json, re, urllib.error, urllib.request

BASE = "http://127.0.0.1:8798"
TOK = open("/tmp/vantage_probe2/api_token").read().strip()
HDR = {"X-JARVIS-Token": TOK, "Host": "127.0.0.1", "Content-Type": "application/json"}


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDR, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


SKILL = "demo-i-report"

print("== 1. save a skill with one checkable and one uncheckable condition ==")
st, saved = call("POST", "/intelligence/skills", {
    "skill_id": SKILL,
    "name": "Write a report",
    "description": "Write a small report file.",
    "steps": [
        {"tool": "write_file",
         "params": {"path": "/tmp/demo_i/report.txt", "content": "a report\n",
                    "mode": "overwrite"},
         "satisfies": "c1"},
        {"tool": "write_file",
         "params": {"path": "/tmp/demo_i/style.txt", "content": "polished\n",
                    "mode": "overwrite"},
         "satisfies": "c2"},
    ],
    "verification": [
        {"condition_id": "c1", "kind": "file_contains",
         "path": "/tmp/demo_i/report.txt", "needle": "a report"},
        {"condition_id": "c2", "kind": "looks_nice"},
    ],
})
print("status", st)
assert st in (200, 201), saved
print("stored version:", saved.get("version"))

print("\n== 2. run it three times -- every run stops at the gate ==")
for i in range(3):
    st, run = call("POST", f"/intelligence/skills/{SKILL}/run", {})
    print(f"  run {i+1}: status {st} state={run.get('state')} "
          f"verdict={run.get('verdict')} version={run.get('version')}")

print("\n== 3. the lab proposes a NARROWING change ==")
st, lab = call("GET", f"/skill-evolution/{SKILL}")
prop = lab.get("proposal") or lab
print("reasons:", prop.get("reasons"))
print("changes:")
for c in prop.get("changes") or []:
    print("  -", c)
print("notes:")
for n in prop.get("notes") or []:
    print("  -", n)
print("evidence:", json.dumps(prop.get("evidence"), indent=1))
print("changes_anything:", prop.get("changes_anything"))
assert prop.get("changes_anything"), "the lab found nothing to change"

print("\n== 4. what the lab can and cannot do ==")
st, overview = call("GET", "/skill-evolution")
print("min_runs:", overview.get("min_runs"))
for k in ("can_change", "will_not_change"):
    print(k + ":")
    for line in overview.get(k) or []:
        print("  -", line)
print("note:", overview.get("note"))

print("\n== 5. accept: store the next version ==")
st, acc = call("POST", f"/skill-evolution/{SKILL}/accept", None)
assert st == 200, acc
stored = acc.get("stored") or {}
print("status", st, "-> stored version", stored.get("version"),
      "risk", stored.get("risk"), "steps", len(stored.get("steps") or []))
print("verification kinds:", [v.get("kind") for v in stored.get("verification") or []])

print("\n== 6. versions: nothing is lost ==")
doc = json.load(open("/tmp/vantage_probe2/skills.json"))
entry = doc["skills"][SKILL]
versions = entry["versions"]
print("stored versions:", len(versions))
for i, v in enumerate(versions, 1):
    kinds = [x.get("kind") for x in v.get("verification", [])]
    print(f"  v{i}: risk={v.get('risk')} steps={len(v.get('steps', []))} "
          f"verification={kinds}")
newest = versions[-1]
print("the uncheckable condition is gone from the newest:",
      "looks_nice" not in [x.get("kind") for x in newest.get("verification", [])])
print("the checkable one is kept:",
      "file_contains" in [x.get("kind") for x in newest.get("verification", [])])
print("the original version is still on disk:", len(versions) >= 2)

print("\n== 7. run the new version, approve it, and look for a verified verdict ==")
st, first = call("POST", f"/intelligence/skills/{SKILL}/run", {})
print("run 1: state", first.get("state"), "verdict", first.get("verdict"),
      "run_id", first.get("run_id"))
conf = None
for step in (first.get("mission") or {}).get("steps", []):
    m = re.search(r"confirmation\s+(\S+)", str(step.get("detail", "")))
    if m:
        conf = m.group(1).rstrip(")")
print("confirmation:", conf)
assert conf, "no confirmation id was recorded"

st, second = call("POST", f"/intelligence/skills/{SKILL}/run",
                 {"approval": {"confirmation_id": conf},
                  "run_id": first.get("run_id")})
print("run 2: status", st, "state", second.get("state"),
      "verdict", second.get("verdict"))
print("reliability:", json.dumps(second.get("reliability"), indent=1))
for c in (second.get("mission") or {}).get("conditions", []):
    print("  condition", c["condition_id"], c["status"], "|",
          str(c["observation"])[:150])
for step in (second.get("mission") or {}).get("steps", []):
    print("  step", step["step_id"], step["status"], "|", str(step["detail"])[:120])

print("\n== 8. the lab re-reads the history after a verified run ==")
st, lab2 = call("GET", f"/skill-evolution/{SKILL}")
p2 = lab2.get("proposal") or lab2
print("reasons now:", p2.get("reasons"))
print("changes_anything:", p2.get("changes_anything"))
print("evidence:", json.dumps(p2.get("evidence"), indent=1))
print("\nDEMO_I_OK")