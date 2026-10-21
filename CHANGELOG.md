# Changelog

Hangar uses semantic versioning (`MAJOR.MINOR.PATCH`). The release number lives in
`manager/__init__.py`; update it and this file for each release.

- Patch: backwards-compatible fixes.
- Minor: new functionality. During `0.x` development, a minor release may also change the public API.
- Major: incompatible public API changes after the stable `1.0.0` release.

Each machine reports its running release and Git revision separately, so two builds of the same release
can still be distinguished. Engine binary versions have their own release numbering.

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
