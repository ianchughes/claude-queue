# Portal Launch Instructions

This is the human-readable contract that the Project Dashboard portal
enforces when launching Claude sessions. The portal and both config
files live together in `C:\Users\ianch\OneDrive\project-dashboard\`:

- `Project Dashboard.pyw` — the portal itself (double-click to launch)
- `projects.manifest.json` — machine-readable project list
- `portal-launch-instructions.md` — this file

All three live in OneDrive so any machine that signs in as `ianch` with
OneDrive sync enabled picks them up automatically — keep the two
machines consistent by editing in one place only. A Desktop shortcut
pointing at the `.pyw` is the easiest way to launch day-to-day.

The portal reads both files every time it refreshes. If you change the
manifest, click **Refresh** (or press F5) to pick up the changes — you do
not need to restart the portal.

## The 7 rules

### Rule 1 — Always `cd` into the project, never launch from `/home/ian`

Every launch command must be of the form

```
cd <cwd> && claude ...
```

where `<cwd>` is the `cwd` field from the manifest for that project. The
portal refuses to launch with `cwd` empty, `/home/ian`, or `/home` — a
bare-home launch was responsible for roughly 43% of token burn before
this rule existed, because Claude would scan every project in `~` on
start-up.

### Rule 2 — Use the manifest to decide what is shown

The portal only shows projects whose `status` is `active` or
`reference`. Anything with `status: archive` or `status: infra` is
hidden entirely — it does not appear in the list and cannot be launched.

### Rule 3 — Reference projects are read-only

Projects with `status: reference` (e.g. `ci-portal-reference`,
`ai-at-ci`, `OKRs`, `knowledge_base`) are historical material. The
portal shows a warning dialog before launching them and the prepended
system prompt tells Claude the project is read-only.

### Rule 4 — Prepend a per-project system instruction

Every Claude session launched by the portal receives a
`--append-system-prompt` flag templated from the manifest entry:

> You have been launched by the Project Dashboard for work in the
> `<name>` project. Current working directory: `<cwd>`. This is the
> `<kind>` (port `<port>`, PM2 `<pm2>`). Owns: `<owns>`. Do NOT cd out.
> If the task requires touching another project, stop and tell the user —
> a separate session should be launched.

Fields that are `null` in the manifest are omitted from the prompt. If
`owns` is filled in well, it becomes the single most useful line in the
session — keep it accurate.

### Rule 5 — SSH launch pattern (remote)

For projects with `location: remote`:

```
ssh -t ian@IansCloudServer "cd '<cwd>' && claude <args>"
```

The portal still accepts the legacy IP `ian@204.168.159.151` as a
fallback; if you have an SSH alias called `IansCloudServer` in
`%USERPROFILE%\.ssh\config`, it is preferred.

### Rule 6 — Local WSL launch pattern

For local WSL launches (used when the project also exists inside the
Ubuntu distro on this Windows machine):

```
wsl.exe -d Ubuntu -- bash -lc "cd '<cwd>' && claude <args>"
```

`bash -lc` (login shell) is required so that `nvm`, `pyenv`, and any
other shell-init-only tooling is available.

### Rule 7 — Sessions must not cross project boundaries

The system prompt in Rule 4 instructs Claude not to `cd` out of the
project. If the user asks Claude to touch a second project during a
session, Claude should stop and tell the user to launch a separate
session from the portal. This keeps each session's context window
scoped to one project and keeps the "which project am I in?" question
trivial.

## Operational notes

- **Editing the manifest:** hand-edit `projects.manifest.json`. Keep the
  JSON valid (trailing commas will break the portal silently — it will
  fall back to scanning-only mode).
- **Adding a new project:** add a new object to the `projects` array
  with `status: active`. Fill in `cwd`, `kind`, `port`, `pm2`, and
  `owns`. The portal will pick it up on refresh.
- **Retiring a project:** change `status` from `active` to `archive`.
  Do not delete the entry — keeping it lets future sessions understand
  why the project is gone.
- **Promoting an archive → reference:** change `status` to `reference`
  and update `owns` to describe the read-only purpose.
