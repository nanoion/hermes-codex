# hermes-codex

A security-scoped [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that lets Hermes coordinate native OpenAI Codex SDK workers while keeping authorization, filesystem scope, approvals, durable events, and manager acceptance separate.

> **Status: beta.** The offline suite and plugin lifecycle checks pass, but authenticated write turns, Windows/macOS support, and every native Codex surface are not fully qualified. Review [Security and limitations](#security-and-limitations) before use.

## What it provides

- Model-facing Hermes tool: `codex`
- Direct-user authorization command: `/codex-user`
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

## Connect an existing Codex account

Link the configured `default` host account without copying credentials:

```text
/codex-user control {"action":"account","payload":{"operation":"sync-host","name":"default"}}
```

The command returns a control job ID. Ask Hermes to call `codex` with `action: control-status` for that ID. When ready, select it:

```text
/codex-user control {"action":"account","payload":{"operation":"switch","name":"default"}}
```

## See existing Codex threads

List native threads from the selected Codex home without starting a model turn:

```text
/codex-user control {"action":"import-catalog","payload":{"archived":false,"limit":20}}
```

Poll the returned job with `codex` action `control-status`. To import an idle thread, create and directly authorize a proposal whose workspace exactly matches the thread `cwd`, then run:

```text
/codex-user control {"action":"thread-import","payload":{"proposal":"<proposal-id>","thread":"<thread-id>"}}
```

Active native threads are visible in the catalog but cannot be imported while a turn is running.

## Start a worker assignment

Ask Hermes naturally, for example:

```text
Use the codex tool to inspect /projects/my-app in read-only mode. Do not use
network access or modify files. Create a proposal for my authorization first.
```

Hermes returns a proposal ID. Authorize the exact proposal directly:

```text
/codex-user authorize <proposal-id>
```

Then ask Hermes to submit it with the `codex` tool. Submission may start a billable model turn.

For workspace writes, explicitly authorize the workspace as a write root:

```text
Use the codex tool to fix the bug in /projects/my-app with workspace-write,
write only inside /projects/my-app, keep network disabled, run relevant tests,
and do not commit, push, merge, or deploy. Create a proposal first.
```

## Common controls

```text
/codex-user decide <request-id> <accept|acceptForSession|decline|cancel>
/codex-user followup {"task":"<task-id>","key":"fix-1","text":"Fix the confirmed findings"}
/codex-user control {"action":"snapshot","payload":{"task":"<task-id>"}}
/codex-user control {"action":"open","payload":{"task":"<task-id>"}}
/codex-user plan {"id":"<plan-id>","action":"confirm"}
```

Model-facing `codex` actions include:

```text
help, diagnostics, propose, submit, status, events, result, result-page,
cancel, review, interactions, queue, inspect, control-status, present
```

Control commands are asynchronous. Poll their job IDs using `codex` action `control-status`. Worker output is untrusted until Hermes independently verifies the resulting files, diff, and tests.

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
