# Why the app cannot drive the desktop (yet), measured

The single fact that has blocked V22's physical gates for several campaigns, now
measured rather than inferred.

## The measurement

One binary, two contexts, different answers:

```
$ macos/Vantage/Vantage.app/Contents/Resources/native/desktop_executor/\
VantageDesktopDev.app/Contents/MacOS/vantage-desktop self_check
{"accessibility": true, "post_events": true, ...}

# the same check, asked by the backend running under Vantage.app:
GET /desktop/windows  ->
{"permissions": {"accessibility": false, "post_events": false,
                 "executable": ".../VantageDesktopDev.app/.../vantage-desktop"}}
```

The executable path is identical in both. The answer differs because macOS does
not grant Accessibility to *a binary* — it grants it to a **responsible
process**, and it attributes the request to the app that launched the binary,
not to the binary itself.

- Launched from a terminal: the responsible process is the terminal, which holds
  the grant, so the executor inherits the answer `true`.
- Launched by the backend, which Vantage.app started: the responsibility chain
  resolves to `com.jarvisforge.vantage`, which holds **no** grant.

## What this means

Granting Accessibility to `VantageDesktopDev.app`, or to the executor binary,
does not help: neither is the responsible process when Vantage.app drives them.
The grant has to exist for **`macos/Vantage/Vantage.app`** itself.

`Vantage.app` is signed **ad-hoc** (`Signature=adhoc`,
`Identifier=com.jarvisforge.vantage`). An ad-hoc signature's code identity
includes the binary hash, so **a rebuild changes it and silently invalidates any
grant**. Rebuilding while chasing this defect can therefore look like the grant
"not working" when the grant was simply orphaned by a new CDHash.

## What is required (a person must do this; nothing here can)

1. Rebuild `Vantage.app` and leave it in place — do not rebuild again afterwards.
2. Open **System Settings → Privacy & Security → Accessibility**.
3. Add `macos/Vantage/Vantage.app` (the app, not the executor, not the terminal).
4. Toggle it on.
5. Relaunch Vantage from Finder so the app starts fresh under the granted identity.
6. Verify from inside the app, not from a shell: the permission readout must show
   `accessibility: true` **when asked by the app's own backend**.

Step 6 is the one that matters and the one that has been conflated before: a
`self_check` run in a terminal reports the *terminal's* capability and proves
nothing about the app.

## What is proven without the grant

Everything that does not require posting input:

- The bundled runtime starts the backend (verified: the process is
  `Vantage.app/Contents/Resources/python/bin/python3.11`).
- The permission engine, approval flow, mission runner, recorder and lease
  checks are all exercised by tests that do not need Accessibility.
- `desktop_move_pointer` and the read-only tools (`desktop_windows`,
  `desktop_identity`, `desktop_permissions`, `desktop_inspect_ui`) are ungated,
  though pointer movement still needs post-event access to take effect.

## What remains genuinely blocked

Pointer move, click, double-click, right-click, typing, shortcuts, scroll, drag,
and any end-to-end desktop mission that posts input — all of them until step 3
above is done. They are `PERMISSION_BLOCKED`, not broken.
