# Changelog

Hangar uses semantic versioning (`MAJOR.MINOR.PATCH`). The release number lives in
`manager/__init__.py`; update it and this file for each release.

- Patch: backwards-compatible fixes.
- Minor: new functionality. During `0.x` development, a minor release may also change the public API.
- Major: incompatible public API changes after the stable `1.0.0` release.

Each machine reports its running release and Git revision separately, so two builds of the same release
can still be distinguished. Engine binary versions have their own release numbering.

## 0.9.0 — 2026-10-25

- Add an independent OpenCode mission plugin with editable objectives, per-objective model assignments,
  automatic local thinker consultations, optional always-on helper support and resumable execution.
- Show mission progress, worker activity and chronological flow in OpenCode's sidebar, with a composer status
  card and activity/results panel. Wingman offers technical consultations during ordinary chats.
- Support optional delegation through installed Codex and Claude Code agents, with local-only defaults and
  explicit consent for cloud consultations.
- Use stable thinker, worker and helper role names in Hangar and coding clients. Worker owns default/fallback
  routing; legacy main requests remain a hidden compatibility alias. Migrate saved main role settings at startup
  and retain completed mission/cycle history while displaying canonical role names.
- Update the dashboard, CLI, MCP descriptions and generated client defaults for the consolidated roles.

## 0.8.1 — 2026-10-25

- Stop telling every client that roles are text-only. The copy-paste client configs (`/api/connect/models`) hardcoded
  `vision: false` for every role, so a client such as opencode refused attachments on a role whose model reads images.
  A role now carries the capability of the model it is set to — this machine's profile (`mmproj`, or the engine's
  `has_vision`) or the vision tag a peer's running engine publishes — and not the capability of the stand-in that
  answers while its own model is down: a client needs the truth before offering a photo the model cannot judge.

## 0.8.0 — 2026-10-23

- Add `/cycle` for omp (`extensions/omp-cycle/`): plan, code, review and fix a task until a fresh review approves it,
  each phase in its own session on its own switching role (`hangar/glm` plans and reviews, `hangar/qwen` codes and
  fixes), so the GPU moves between them. The planner and the reviewer only get read-only tools - a review that edits
  files voids its own approval - and a step answered by a fallback model stops the run unless `--allow-fallback`.
  Runs are resumable and keep their journal (state, plan, each review, test output) in `~/.omp/agent/cycle/runs/`;
  models, rounds, tests and reasoning effort live in `~/.omp/agent/cycle.json`. Nothing is committed.
- Add `scripts/omp-cycle`, the same loop as one command from a terminal: one shared omp session for every step,
  `--test <cmd>` after each coding step, `--resume last` to carry an interrupted run on, and `--ship` to commit, push,
  open a PR and merge it - only for an approved run with passing tests, and never on main/master or a detached HEAD.
- Test the cycle's rules and its git/test handling without omp (`node --test extensions/omp-cycle/`).
- Refresh the peer routing test for the rule 0.3.0 introduced: direct engine health decides whether a peer stays
  routable, with no fixed grace window (the test still asked for that window and failed to import ever since).

## 0.7.0 — 2026-10-23

- Add the machine log: every engine's log, the own log of an engine that keeps one (Maya's, Strata's), and Hangar's
  events as one tagged stream that follows a switch from one engine to the next without picking a log
  (`GET /api/machine-log[/stream]`, `hangar logs [-f]` with no engine, the dashboard's Machine log button).
- Show model changes as banners in it, telling a switch apart from a plain stop or start, a return home or a crash.
- Follow logs without losing or garbling lines: rotation, unfinished lines, characters split across reads, and
  engines starting or exiting while the stream opens.

## 0.6.1 — 2026-10-22

- Name a switching role after its own model (`glm → maya-maya-m on my-pc (switches)`) in `/v1/models` and client
  configs, rather than after the fallback answering while that model is not loaded.

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
