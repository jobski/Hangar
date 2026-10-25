# Missions and Wingman for OpenCode

An independent OpenCode 2.0.32 plugin with editable objectives, model assignments, workers and execution history in the right sidebar. The OMP extension is unchanged.

## Defaults

- Local only: `hangar/worker` implements, `hangar/thinker` plans and advises, and `hangar/helper` provides read-only file discovery alongside the planner.
- Helper runs independently of the worker/thinker switching pair. Its findings are passed to subsequent objectives as supporting context.
- Cloud mission assignments require explicit cloud enablement. Normal-chat Codex/Claude backups require consent for each consultation. A local model failure never enables cloud execution.
- A mission starts with planning and implementation. PaperCut repositories also receive check/review objectives. In other repositories those objectives require explicit enablement in settings.
- Native file edits and shell commands retain OpenCode permission prompts. Planning, Helper and expert consultations have enforced read-only tool lists; reviewers also receive the current diff.
- No automatic commits, pushes or publishing. Repository instructions continue to apply.

These are model-independent Hangar role IDs. Change a role's target in Hangar to replace its model. Older saved defaults and unfinished objective assignments using `hangar/glm` or `hangar/qwen` migrate to `hangar/thinker` and `hangar/worker`. The UI also uses the current names for historical assignment labels; objective/worker details show the original recorded role ID. Completed assignments, results and stored event text remain intact. Click an event in the activity panel to inspect its original text.

## Load the plugin

OpenCode supplies the plugin runtime and Solid UI. No dependency installation in this repository is required.

For a project-local installation, create a directory symlink under the project's `.opencode/plugins/` pointing at this extension. OpenCode 2 discovers directory plugins for both server and CLI. For this repository, from its root:

```sh
mkdir -p .opencode/plugins
ln -s ../../extensions/opencode-mission .opencode/plugins/hangar-mission
```

Run these only when you want to install it; implementation does not create the link. Keep the local link out of commits unless deliberately adopting project-wide installation.

Alternatively, add the extension's absolute directory to the `plugins` array in both `opencode.json` (server) and `cli.json` (sidebar). Preserve the rest of each configuration. Back up user configuration before an approved edit, especially configurations containing credentials. Restart the server and terminal after installing.

The Hangar provider must already expose `helper`, `worker` and `thinker`. Retain the existing provider credentials; the plugin does not store them in mission journals. It uses the authenticated request to discover and warm the requested role.

## Workspace setup

Missions use Git working trees for workspace snapshots, diffs and review validation. Creating or starting a mission
in a plain folder automatically runs `git init -b main`. Existing repositories, parent repositories and worktrees
are retained. No initial commit is created. A paused mission in a plain folder also initialises Git when resumed;
use `/mission resume`. Other Git failures are reported rather than treated as permission to initialise a repository.

## Commands

| Command | Action |
|---|---|
| `/mission <task>` | Create the default objectives and start a one-shot mission |
| `/mission` | Open the mission menu to inspect, edit and start/resume |
| `/mission status`, `/mission activity` or `/mission flow` | Open live activity, objective results and history |
| `/mission watch` | Open the active worker session directly |
| `/mission workers` or `/mission-workers` | Inspect active workers and every past attempt |
| `/mission output compact`, `verbose` or `hidden` | Set the live output pane display without pausing execution |
| `/mission-output` | Choose the live output display mode |
| `/mission start` or `/mission resume` | Execute pending objectives |
| `/mission pause` | Interrupt active work and keep its changes and journal |
| `/mission cancel` | Stop this mission; existing changes remain |
| `/mission add` | Append an objective and completion criteria |
| `/mission settings` or `/mission-settings` | Change default models, Helper and execution policy |
| `/mission help` or `/mission-help` | Show setup, review-loop and model-assignment guidance |
| `/mission-add` | Open Add objective directly |
| `/wingman-settings` | Open the shared model and policy settings |
| `/wingman on` | Enable interactive assistance and select the configured OpenCode worker model |
| `/wingman off` | Disable assistance and interrupt an active consultation |
| `/wingman consult [question]` | Request a read-only expert consultation at the next idle boundary |

Typing `/mission` now lists help, settings and add-objective shortcuts with descriptions in slash completion. The mission menu also includes Models and settings and Help. The command palette provides the same mission, settings, flow, pause and consultation actions. Sidebar entries open objective and worker details; keyboard-only users can use `/mission` to select the same objectives.

`/mission <task>` starts the default objective sequence immediately. Use `/mission` or the sidebar's New mission action to create a mission for inspection before starting it. When no unfinished mission or enabled Wingman is present and both chats are idle, a command in another chat moves control there while retaining history. An unfinished mission stays with its controlling chat; use the sidebar link to open it. Action failures show the reason rather than a generic RPC error. Completed objectives are immutable history. Pending objectives can be edited, moved or skipped. Adding an objective to a completed mission reopens it. Changing an active objective first pauses and interrupts it; resume creates a fresh worker that sees the current workspace and earlier results.

Each objective has its own executor, model, completion criteria and permission profile. Default-role changes affect new objectives; existing assignments are changed through the objective menu. Permissions remain independent of the model. A later code change after a completed review appends a fresh review when reviews are enabled.

## Seeing what is happening

A live output pane sits above the composer in the controlling conversation and its worker sessions. Compact mode
shows the current objective/model, elapsed time, last-event age and three recent output lines. Verbose mode adds a
scrollable, bounded view of worker response text and tool results, with combined stdout/stderr streamed from
OpenCode's captured shell output. Scroll up to stop following, then use **Jump to latest** to follow again. Hidden
mode retains the status line and controls and stops fetching output. The mode is remembered across terminal UI
restarts and does not change execution, model selection or permissions.

The animated indicator means the mission is running; it is not a percentage or evidence of fresh output. Loading,
model generation, commands and approval/question waits have explicit text states. Quiet periods retain the last
worker-event age; streaming command output has its own last-output age. Only the selected mission worker's native
messages and shell captures are read. Reasoning text is excluded. Displayed output is bounded to 32 KiB, terminal
control sequences are removed and common credential assignments/Bearer tokens are redacted. Full native output
remains in the worker session.
Use **Watch** to jump directly to the active worker, **Activity** for a scrollable view of objectives, results, worker output and history, or **Pause / Resume** to control execution. The activity panel can expand to fullscreen. Completed and paused missions keep their summary visible, including the stop reason when one is available. Native permission requests and questions are marked **Needs your input** and link to the worker session.

The sidebar lists active workers separately. Past attempts are collapsed under a History summary with interrupted, failed and completed counts. Expand it to inspect recent attempts, or use `/mission workers` for the full history and native worker sessions. Interrupted attempts retain their final status; resuming creates a new worker.

Stage changes also show brief toast notifications. Worker output previews contain partial response text; reasoning token events update the activity indicator without exposing reasoning text. A quiet-period notice reports the lack of new events, without claiming the worker has failed. These displays are UI-only: they do not enqueue chat prompts, start extra model turns or switch models. Full worker output remains available in the native worker session.

## Handoff to the main chat

Mission runs objectives in child worker sessions, while the controlling chat keeps its own tool history. On each
normal top-level chat turn in the same workspace, the plugin supplies a bounded report from the current mission
journal: task/status, objective results, model assignments and worker session references. The same report is
included during compaction. This also works for missions completed before the handoff feature was installed.

The chat can explain what Mission built without claiming its own session executed those tools. Checks/reviews are
labelled as worker-reported evidence; a completed mission does not imply a commit or deployment. Reports are
refreshed from persisted state rather than copying entire worker conversations or reasoning. Later file edits can
make reported results stale. Merely completing a mission does not queue an extra model turn; the handoff is
available when you next speak to the chat.

## Questions in normal chat

Outside missions, the worker is instructed to use `ask_technical_question` when unresolved technical uncertainty would otherwise require your input. Its native question prompt offers **Ask Thinker (local)** first, followed by **Ask Codex (cloud)** and **Ask Claude Code (cloud)** when their installed CLIs report a signed-in account. You can answer directly instead. Routine choices remain with the worker; preferences, requirements and approvals use the ordinary question prompt.

The question and focused context are shown before you choose. Advisers receive that context and the current workspace diff, and can read project files. Choosing a cloud backup authorises that single consultation, using the agent's configured default model; it does not enable cloud execution for other work. Availability checks only inspect local login status and cannot guarantee quota or network availability. A failed local consultation never starts a cloud backup automatically.

Consultations are read-only. Thinker loading and the current Hangar model's return are covered by the existing readiness/serving-identity checks. Advice returns through the tool to the same worker, labelled as advice rather than user authorisation, and the expert appears in workers and flow. New user input interrupts the consultation. Declined offers are remembered per question and conversation; the same offer is not repeated. The feature can be disabled under **Technical question offers** in mission settings and does not require Wingman to be enabled.

## Waiting, assistance and recovery

Before requesting inference, the plugin checks the role's explicit primary target, asks Hangar to warm it if needed, then waits for readiness. The sidebar distinguishes loading from working. The loading deadline is ten minutes; objective execution is bounded separately. Responses without a verifiable serving identity, or from a fallback, are rejected. An unavailable Helper is optional and does not fail the main objective.

Wingman queues expert help when the worker asks, or when the same shell command fails twice. A successful command clears its failure counter. Consultation waits for the main turn to finish, so the switching pair is used serially. Advice is returned as a synthetic session message and the worker continues. New user input interrupts a consultation or pauses a mission; input is preserved.

The default budgets are three attempts per objective and three expert consultations per mission or Wingman session; both are adjustable in settings. Infrastructure and transport failures pause execution without consuming a coding attempt. If a worker omits its structured result, it receives one bounded follow-up turn with only result recording available. Failed check objectives can insert fixes; reviews require explicit approval or findings, with repeated findings stopping the loop.

State, results and flow are stored in OpenCode's plugin storage outside the repository. Native workers keep their full OpenCode sessions. Reopening the plugin interrupts recorded unfinished native workers and leaves a running mission paused for explicit resume. External agent work is restarted as a fresh read-only patch proposal with previous results and the current diff; their native session references are recorded, but native agent-session resume is not yet exposed.

Run one OpenCode server per workspace. Scheduling locks cover controllers within that server; they do not coordinate independent servers or unrelated clients using the same GPU. Cancelling a mission stops its requests and future work, but does not undo changes or promise to stop a load Hangar has already started.

## Codex and Claude Code

Enable cloud execution in settings, then choose an external agent for an objective or the expert role. Each executable uses its own installed authentication and model availability. No API credentials are copied into OpenCode or Hangar.

- Codex runs via `codex exec --json` in a read-only sandbox.
- Claude Code runs via `claude -p` with read/glob/grep tools and no permission bypass. MCP servers are disabled for that invocation.
- Implementation objectives ask the actual agent to prepare a git-style text patch. The host rejects protected paths, symlinks, unsupported patch formats and bulk removal, checks for workspace changes, requests OpenCode edit permission, then applies the patch.
- External agents do not run verification commands in this first adapter. Assign a separate local check objective when permitted.
- Agent permission denials or unsupported operations stop the objective; there is no automatic approval bridge to the agent's native UI.

Patches currently require unquoted paths without spaces and regular text files. Binary, rename, copy and symlink patches need manual handling. Local-only remains the initial policy even when these executables are installed.

## Verification status

The implementation compiled against published 2.0.32 types and loaded through the real server/CLI runtime in an isolated configuration. The sidebar, objective RPC, and a read-only objective on `hangar/helper` were exercised without changing the owner's normal configuration. The helper response was checked against Hangar's served-by identity. No test suites or review stages were run under this personal repository's rules.

The central mission card and scrollable activity panel were also rendered in the real terminal UI using a ready mission in an isolated configuration, without starting inference. Live text/tool activity is captured from native worker events; permission waits and stage notifications use the same persisted state.

The normal-chat question extension also compiles and loads in that runtime. Its native question selection and thinker/cloud advice handoff have not yet been exercised live. The implementation adds a dedicated technical-question tool and model instruction; it does not classify arbitrary prose questions or permission prompts after they are displayed.

Live worker/thinker switching and cloud-agent execution require separate operational verification. The original handover reports that streaming through Hangar works with OpenCode; this plugin adds explicit warming and primary-target checks. Do not infer successful execution of an objective from plugin loading alone.
