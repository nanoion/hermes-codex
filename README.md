# hermes-codex

A security-scoped [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that lets Hermes coordinate native OpenAI Codex SDK workers while keeping authorization, filesystem scope, approvals, durable events, and manager acceptance separate.

> **Status: beta.** The offline suite and plugin lifecycle checks pass, but authenticated write turns, Windows/macOS support, and every native Codex surface are not fully qualified. Review [Security and limitations](#security-and-limitations) before use.

## What it provides

- Model-facing Hermes tool: `codex`
- Simple direct-user controls: `/codex status`, `/codex approve`, `/codex continue`, `/codex result`, and `/codex cancel`
- Advanced compatibility command: `/codex-user`
- Read-only and workspace-write assignments with explicit roots
- Proposal → direct authorization → submission workflow
- Durable task status, events, paged results, follow-ups, cancellation, approvals, plans, reviews, goals, and attachments
- Existing Codex account/thread discovery through a named host account home
- Credential redaction before durable stream persistence
- Filesystem identity revalidation before queued steering
- Profile-local state under `$HERMES_HOME/codex-worker/`

## Requirements

- Linux
- Python 3.11+
- Hermes Agent 0.21.3 or newer
- `git`, `python3`, and `pip`
- An existing Codex login if using `host_account_homes`, normally `~/.codex`

## Quick install

Clone once:

```bash
git clone https://github.com/nanoion/hermes-codex.git
cd hermes-codex
```

Hermes 0.21.3 needs the direct plugin-command session-context compatibility fix. It is idempotent, creates `.hermes-codex.bak` backups, and refuses unknown source layouts:

```bash
python3 scripts/patch-hermes-session-context.py
```

Install and enable the plugin for a named profile:

```bash
./scripts/install.sh code-agent
```

Install it for another profile by running the same command with that profile name:

```bash
./scripts/install.sh codex-agent
```

The installer:

1. Copies only runtime/plugin files.
2. Vendors `openai-codex==0.157.0` and its pinned CLI locally inside the plugin.
3. Does **not** replace packages in Hermes' shared virtual environment.
4. Runs `hermes plugins validate` and `hermes plugins doctor` before activation.
5. Enables the plugin without built-in tool override permission.
6. Preserves an existing installation as a timestamped backup.

Restart the affected gateway from an external shell:

```bash
systemctl --user restart hermes-gateway-code-agent.service
```

Then send `/reset` in that Hermes chat. Do not use `sudo` with `systemctl --user`.

## Configure securely

The plugin defaults to no allowed workspace and `allow_full_access: false`.

Example for profile `code-agent`:

```bash
export HERMES_HOME="$HOME/.hermes/profiles/code-agent"

hermes config set \
  plugins.entries.hermes-codex.settings.allowed_roots \
  '["/projects"]'

hermes config set \
  plugins.entries.hermes-codex.settings.allow_full_access \
  false

hermes config set \
  plugins.entries.hermes-codex.settings.host_account_homes \
  "{\"default\":\"$HOME/.codex\"}"
```

Use absolute, existing, non-symlink paths. Restart the profile gateway and send `/reset` after configuration changes.

`allow_full_access: true` is only an operator availability gate. A full-access proposal is still rejected unless `/` itself is in `allowed_roots`, the assignment explicitly requests `/`, unrestricted networking is authorized, and the direct user authorizes that exact proposal.

## Everyday use

Talk to Hermes normally. The plugin adds workflow guidance so Hermes discovers current Codex work, calls the `codex` tool, follows asynchronous controls, and summarizes results without exposing internal JSON.

Examples:

```text
Show me what the current Codex task is doing.
Continue the current Codex task and run the regression tests.
Inspect the existing Codex threads for /projects/my-app.
Ask Codex to fix the login bug in /projects/my-app. Do not commit or deploy.
```

For write-capable work Hermes creates an exact scoped proposal. Confirm once:

```text
/codex approve
```

Approval starts the task immediately. An ID is needed only when several proposals or tasks are eligible.

## Simple `/codex` controls

```text
/codex                         # current work and next actions
/codex status                  # latest active task
/codex approve                 # approve and start the only pending proposal
/codex continue <instruction>  # continue the current task
/codex result                  # latest result page
/codex events                  # recent durable events
/codex cancel                  # cancel the current task
/codex list                    # concise task/proposal summary
```

Add a task/proposal ID only to disambiguate, for example:

```text
/codex status <task-id>
/codex approve <proposal-id>
/codex continue <task-id> run the focused tests again
```

The plugin never guesses when more than one authorization target is available.

## One-time account setup

Link the operator-configured `default` Codex home without copying credentials into the plugin:

```text
/codex-user control {"action":"account","payload":{"operation":"sync-host","name":"default"}}
```

Then ask Hermes to check the control job and switch to the ready account, or use the advanced compatibility command:

```text
/codex-user control {"action":"account","payload":{"operation":"switch","name":"default"}}
```

`/codex-user` remains available for advanced controls and compatibility. Routine task use should prefer natural chat and `/codex`.

## Existing Codex threads

Ask Hermes naturally:

```text
Show my current Codex threads and summarize what each is doing.
Continue the idle thread for /projects/my-app and inspect the failing tests.
```

Hermes uses read-only inspection and bounded control polling. Importing an idle native thread still requires an exact workspace proposal and direct confirmation. Active native threads can be inspected but are not imported while a turn is running.

Model-facing `codex` actions include:

```text
help, list, diagnostics, propose, submit, status, events, result, result-page,
cancel, review, interactions, queue, inspect, control-status, present
```

Worker output remains untrusted until Hermes independently verifies resulting files, diffs, and tests.

## Development

Create an isolated environment and install the pinned SDK:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

Run the offline suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q hermes_codex tests
.venv/bin/python -m pip check
```

Validate an installed plugin against Hermes:

```bash
export HERMES_HOME="$HOME/.hermes/profiles/code-agent"
hermes plugins validate "$HERMES_HOME/plugins/hermes-codex"
hermes plugins doctor "$HERMES_HOME/plugins/hermes-codex" --ci
```

Live/native tests are opt-in and may require authentication or billable calls. They are skipped by default.

## Security and limitations

- Authorization and approval decisions are direct-user commands, not model tool actions.
- The plugin derives ownership from trusted Hermes session context; caller-supplied owner IDs are rejected.
- Credentials must never be placed in assignment text, identifiers, paths, logs, or chat.
- Pattern-based credential redaction is defense in depth, not a substitute for secret hygiene.
- Linux-only primitives are currently used (`fcntl`, no-follow filesystem operations).
- Runtime environment masking is not complete OS-level isolation.
- Existing historical databases are not rewritten when redaction logic changes.
- Full Codex feature parity and production acceptance are not claimed.
- Do not expose the plugin to untrusted Hermes users without reviewing profile access controls.

See [SRS.md](SRS.md), [CODEX_FEATURE_PARITY.md](CODEX_FEATURE_PARITY.md), and [PROGRESS.md](PROGRESS.md) for detailed scope and remaining gaps.

## Uninstall

Disable the plugin for the profile:

```bash
export HERMES_HOME="$HOME/.hermes/profiles/code-agent"
hermes plugins disable hermes-codex
```

After stopping/restarting the affected gateway, remove `$HERMES_HOME/plugins/hermes-codex` if you no longer need the installed runtime. Preserve `$HERMES_HOME/codex-worker/` if task history or reconciliation state is still required.

## License

Copyright 2026 Thee Tienboon.

Licensed under the [Apache License 2.0](LICENSE). Third-party packages installed by the installer retain their own licenses.
