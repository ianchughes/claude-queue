# Project Dashboard

The Tkinter portal that launches Claude sessions against the right
project with the right system prompt. See
[`portal-launch-instructions.md`](portal-launch-instructions.md) for
the 7-rule contract the portal enforces.

## Files

- `Project Dashboard.pyw` — the portal itself
- `projects.manifest.json` — 26 projects classified as active / archive / reference / infra
- `portal-launch-instructions.md` — human-readable contract
- `install-project-dashboard.py` — self-contained installer with all three files base64-embedded

## Install on a new machine

Pull this folder, then run the installer:

```
py project-dashboard\install-project-dashboard.py
```

It writes the three files to `%OneDrive%\project-dashboard\` and drops
a `Project Dashboard.lnk` shortcut on your Desktop. Double-click the
shortcut to launch.
