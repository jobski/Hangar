# Changelog

Hangar uses semantic versioning (`MAJOR.MINOR.PATCH`). The release number lives in
`manager/__init__.py`; update it and this file for each release.

- Patch: backwards-compatible fixes.
- Minor: new functionality. During `0.x` development, a minor release may also change the public API.
- Major: incompatible public API changes after the stable `1.0.0` release.

Each machine reports its running release and Git revision separately, so two builds of the same release
can still be distinguished. Engine binary versions have their own release numbering.

## 0.6.0 — 2026-10-22

- Add switching roles: a request for one loads its model on its machine (stopping what holds the GPU) and waits for it,
  with loading progress every 10 s on a stream that opencode, Qwen Code and omp all accept; progress is stripped from
  the history they send back. A hub relays the request to that machine and labels any fallback answer.
- Keep a switched-in model for a minimum turn, back off after a failed start, return an idle one to the machine's home
  model, and bound the wait (`/api/switching`, `hangar switching set ...`).
- Set it all up in one action (`hangar switching setup <machine> <thinker> <coder>`, `POST /api/switching/setup`) and
  write the clients' plan/build/small slots with Connect (opencode agents, omp `modelRoles`).
- `hangar switch <role>` and `POST /api/warm` load a role's model ahead of the first request.

## 0.5.0 — 2026-10-22

- Let a running model finish its reply before a switch stops it (`resources.drain_timeout_s`, 180 s); requests for it
  meanwhile get `503` with `Retry-After` and `X-Hangar-State: stopping`, and a hub no longer relays around the drain.
- Drop a queued start that a newer conflicting start would undo at once (last switch wins), re-checked after the drain;
  custom runs take part, and a start that is itself dropped cancels nothing.
- Keep Maya's conversations across restarts and stop Maya cleanly through a new engine stop hook; on Windows it first
  sets the conversation it holds aside on disk.
- Count tokens generated during a drain in usage and on the activity chart.

## 0.4.0 — 2026-10-22

- Name each role after the model serving it in `/v1/models` and the Connect configs, flagging a fallback, with its context size.
- Mark roles served by Strata or Maya as reasoning models in generated client configs.
- Add an Oh My Pi (omp) client config, written to `~/.omp/agent/models.yml`.
- Merge applied client configs by model id, keeping each model's other settings instead of replacing them.
- Write `hangar connect --apply` on the machine running the command rather than the manager's machine.

## 0.3.0 — 2026-10-21

- Keep routing directly to healthy peer engines while their manager is offline, without a fixed grace window.
- Check engine health in the background and refresh it from direct requests; recover normal routing when the manager returns.
- Use direct engine health for backup supervision and spill routing, with separate manager/engine status in the dashboard.
- Show Strata's reported tok/s during budgeted thinking as well as answering and tool-call generation.

## 0.2.0 — 2026-10-21

- Show running Hangar versions and Git revisions on machine cards and the selected machine's resource panel.
- Flag different builds across online machines and show inference engine versions separately.
- Record when a build first starts and when the current manager starts; retain the update time across restarts.
- Report the semantic release through `hangar --version`, `/api/ping` and OpenAPI.

## 0.1.0

Initial development version. Earlier changes were identified by Git revision; no release date is recorded here.
