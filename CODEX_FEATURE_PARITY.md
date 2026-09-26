# Codex Feature Parity — normative SRS companion

## Scope and baseline

This catalogue implements confirmed requirement UR-04: **all Codex functionality in `/projects/telegram-codex-app-bridge` shall be supported by hermes-codex**, including shared bridge workflows used by Codex. It is normative under SRS FR-16 and AC-13.

Baseline: inspected working tree based on commit `3cf5cbddd8de26283b37f4b8c879242751ddf042`, INCLUDING modified and untracked source such as native Goals. The commit alone does not identify the complete baseline. `REFERENCE_BASELINE.json` records source hashes for reproducibility. Runtime data, credentials, generated files, and unrelated engine implementations are not copied.

This is a source-backed requirements inventory, not a claim that the reference app or Python SDK has passed runtime tests. All entries below currently have **implementation: not started; SDK compatibility: unverified; live acceptance: not run**.

## Parity rules

- Preserve behavior, not Telegram-specific transport code. Hermes remains manager and user-facing channel owner; Codex remains worker.
- Telegram command names below are reference identifiers. Equivalent discoverable Hermes tools/actions and native interactions are acceptable; requiring the user to return to the old bridge is not.
- Each action, callback path, configurable behavior, scope guard, and recovery outcome belongs in the acceptance inventory, not only commands listed in README.
- Missing SDK functionality is a gap to resolve, not permission to hide or remove a feature. SDK-supported lower-level calls may be evaluated; a raw-protocol replacement or sidecar bypass requires an explicit architecture decision.
- Preserve supported behavior without copying security weaknesses. Privileged operations require scoped authorization; worker output never creates authorization.
- A feature requiring a desktop, server flag, account, or platform integration remains in scope. Report prerequisites honestly and test on a suitable environment before claiming parity.
- New features subsequently added to the reference require a baseline update; this document does not silently promise all future Codex capabilities.

## Required feature catalogue

### CP-01 — Help and action discovery

**Source:** `src/controller/telegram_ingress.ts` commandHandlers/isCommandSupported; `src/telegram/gateway.ts`; README Commands.

Support `/start`, `/help`, action discovery, aliases, contextual navigation, and capability-aware help. All required Codex functions shall remain discoverable even when their environment prerequisites are unavailable, with an explicit readiness explanation.

**Acceptance:** Enumerate every registered Codex command and callback and map it to an available Hermes action or documented alias; no unmapped behavior remains.

### CP-02 — Thread creation, opening, and binding

**Source:** `src/controller/thread_session.ts`, `telegram_ingress.ts`; `src/codex_app/client.ts` thread/start, thread/resume, thread/read.

Support `/new`, `/open`, explicit thread IDs and reference-list selections, workspace selection, persistent scope binding, existing-thread resume, and validated stale/missing-thread recovery. Do not silently substitute a new thread or lose manager context.

**Acceptance:** Create, open, resume, and recover bindings across restart; reject invalid IDs and cross-profile references without misrouting work.

### CP-03 — Thread browsing and history

**Source:** `src/controller/thread_panel.ts`, `threads_panel.test.ts`, `presentation.ts`.

Support `/threads`, search, next/previous pages, clearing filters, active/archived views, current-thread indication, and recent history previews. Distinguish complete, partial, interrupted, and failed history rather than presenting incomplete output as a final result.

**Acceptance:** Browse a multi-page filtered list, switch archived/active views, open a selected result, and display its real history and status.

### CP-04 — Rename, archive, and unarchive

**Source:** `src/controller/thread_panel.ts`, `thread_rename.test.ts`; client thread/name/set, thread/archive, thread/unarchive.

Support rename draft/input/confirm/cancel, archive/unarchive confirmation, configured archive permission, active-turn guards, and binding cleanup after archiving the current thread. Preserve validation and stale-action protection.

**Acceptance:** Confirm and cancel each mutation, verify server read-back, and test active-turn, stale-selection, and permission failures.

### CP-05 — Codex desktop reveal/focus and synchronization

**Source:** `src/codex_app/deeplink.ts`, `client.ts` revealThread; `src/controller/thread_session.ts`, `thread_panel.ts`; platform capabilities.

Support `/reveal`, `/focus`, configured app launch, sync-on-open and sync-on-turn-complete behavior on supported desktop hosts. Headless environments shall report the missing desktop capability without falsely claiming success.

**Acceptance:** On a qualified desktop host, reveal the exact selected thread and exercise both sync settings. Separately test headless failure reporting; that negative test alone is not desktop parity.

### CP-06 — Turn execution and live output

**Source:** `src/controller/turn_execution.ts`, `turn_lifecycle.ts`, `turn_rendering.ts`, `activity.ts`, `turn_completion.ts`.

Support asynchronous text/image turns, configured developer instructions, streamed worker text, tool activity, exposed progress events, and distinct final results. Preserve output sanitation, length handling, update coalescing, stale-event isolation, and visible completion/error delivery. Activity summaries shall not expose private reasoning or treat commentary as final completion.

**Acceptance:** Run real text and image tasks; verify ordered progress, long output handling, command/file activity where exposed, and final versus interrupted/failed outcomes.

### CP-07 — Interrupt and stop

**Source:** `src/controller/turn_execution.ts`, `turn_lifecycle.ts`; client turn/interrupt.

Support `/interrupt`, `/stop`, and task-specific stop controls, including native-goal turns. Resolve the exact turn and distinguish requested interruption from confirmed termination. No automatic rollback claim.

**Acceptance:** Stop the selected active turn, reject stale/cross-task stop controls, and retain unknown status when termination cannot be confirmed.

### CP-08 — Durable queue and active-turn steering

**Source:** `src/controller/turn_queue.ts`, `turn_guidance.ts`, `queue.test.ts`, `turn_guidance.test.ts`; client turn/steer.

Support `/queue`, ordered automatic follow-up queuing, auto-queue on/off, cancel-next and clear queue, recovery, `/guide`, and the steer-now versus keep-queued choice. Preserve attachment-bearing queued input and prevent duplicate execution when moving input from queue to active steering.

**Acceptance:** Queue multiple follow-ups, cancel one, steer another into the exact running turn, retain another for later, restart, and verify order and no duplication.

### CP-09 — Model discovery and selection

**Source:** `src/controller/settings.ts`, `src/codex_profiles.ts`; client model/list.

Support `/models`, `/model`, paginated model discovery, defaults, configured model variants where applicable, validation, and persisted scope-specific preferences. Distinguish a pending configured preference from the effective model on an existing thread.

**Acceptance:** List all available model pages, select and persist a valid model, reject invalid selections, and verify effective runtime settings.

### CP-10 — Reasoning effort, service tier, and fast mode

**Source:** `src/controller/settings.ts`, `settings.test.ts`, `src/codex_profiles.ts`.

Support `/effort`, `/tier`, `/fast`, server defaults, and provider/model capability validation. Do not silently downgrade, escalate, or advertise an unsupported value as applied.

**Acceptance:** Exercise all values allowed by the qualified provider/model; verify passed/effective settings, defaults, persistence, and unsupported combinations.

### CP-11 — Provider-profile routing and switching

**Source:** `src/codex_profiles.ts`, `src/controller/settings.ts`, `src/engine/codex_provider.ts`.

Support `/provider`, configured profile discovery, per-scope selection, independent client lifecycle, profile defaults and capability flags, backend identity reporting, and switch blockers for active/pending work. Preserve provider-specific thread bindings/settings as implemented by the reference; do not move a thread to the wrong account/backend.

**Acceptance:** Switch between two qualified Codex profiles, verify correct routing and isolation, and test busy/approval/input/plan/queue blockers. Non-Codex engines are not required by this entry.

### CP-12 — Account login, listing, and switching

**Source:** `src/controller/telegram_ingress.ts` login handlers, `login_command.test.ts`; `src/codex_app/account_manager.ts`, `auth_state.ts`, `credential_store.ts`, `oauth_device.ts`.

Support the reference `/login` behavior: device login start/continue/status, expiry and cancellation, saved account list, selection/switching, host-auth synchronization, token refresh as needed, and protected credential storage. Show sanitized identity and plan information without leaking credentials. Switching requires trusted authorization and safe handling of ongoing work; do not mutate another Hermes profile's credentials.

**Acceptance:** Complete an authorized login through secure UI, cancel/expire another, list and switch saved test accounts, verify effective identity, and exercise invalid refresh/error handling with no secret leakage.

### CP-13 — Settings, location, and access presets

**Source:** `src/controller/settings.ts`, `access.ts`, `status_command.ts`.

Support `/settings`, `/where`, `/permissions`, `/access`, effective workspace/thread/provider/model settings, read-only/workspace-write/full-access presets as supported by the reference, explicit approval policy, and configured/effective distinction. Full-access availability is not authorization to enable it; higher-risk changes require explicit trusted approval and enforceable boundaries.

**Acceptance:** Inspect and change each supported setting, verify persistence/effective policy, and prove that denied escalation and workspace escape remain blocked.

### CP-14 — Native plan and collaboration modes

**Source:** `src/controller/settings.ts`, `thread_session.ts`; client startTurn collaborationMode.

Support `/mode`, `/plan`, entry/exit and prompt aliases, plan versus execution semantics, and mode persistence. Planning shall not silently execute proposed changes.

**Acceptance:** Generate a plan without edits, change modes under authorization, and verify the correct mode reaches Codex.

### CP-15 — Guided plan lifecycle and history

**Source:** `src/controller/guided_plan.ts`, `guided_plan.test.ts`, `turn_guidance.ts`; `src/store/plan_state_repository.ts`.

Support a live evolving plan, confirm/continue, revise, cancel, focused follow-up, plan-confirmation and history settings, persisted plan history, and recovery show/continue/cancel. Preserve pending workflow guards and prevent stale confirmations from executing a newer plan.

**Acceptance:** Complete confirm/revise/cancel branches, restart with a pending plan, recover all supported choices, and verify no execution precedes required confirmation.

### CP-16 — Structured user input

**Source:** `src/controller/pending_user_input_coordinator.ts`, `pending_input.test.ts`, `pending_flow.test.ts`; ingress item/tool/requestUserInput.

Support structured questions/options, free-text/Other, back, answer editing, submit, cancellation, and pending-input recovery. Bind each response to the exact outstanding request and retain the distinction between answering a question and authorizing an operation.

**Acceptance:** Answer and edit a multi-question request, use Other, cancel, recover after restart, and reject duplicate/stale/wrong-owner replies.

### CP-17 — Command and file-change approvals

**Source:** `src/controller/approval_coordinator.ts`, `approval_rendering.ts`, `approval.test.ts`; ingress commandExecution/requestApproval and fileChange/requestApproval.

Support command/file-change approval details, summary/detail navigation, accept once, session-scoped acceptance where permitted, deny, expiry denial, and restart recovery. Preserve reason, command/path, cwd, risk/details, and request/turn/item binding. Session approval shall not imply global future permission.

**Acceptance:** Exercise both request types and every decision, including timeout, reconnect/restart, stale callback, duplicate decision, and cross-scope denial.

### CP-18 — Native Codex code review

**Source:** `src/engine/types.ts` ReviewTarget/ReviewDelivery; `src/controller/turn_execution.ts`, `review_command.test.ts`, `thread_session.ts`; client review/start.

Support `/review` and native review targets: uncommitted changes, base branch, commit with optional title, and custom instructions; preserve inline and detached delivery supported by the provider interface and review thread/turn tracking. Native worker review does not replace Hermes acceptance of the delivered task.

**Acceptance:** Execute each review target and delivery form with actual result identifiers, handle invalid inputs, and independently record manager acceptance separately.

### CP-19 — Native Codex Goals

**Source:** README Native Codex goals; `src/controller/goal.ts`, `goal.test.ts`, `commands.ts`; `src/engine/goals.ts`; client thread/goal/get, set, clear.

Support objective set/get/status, pause, resume, clear, and positive-integer total token budget. Preserve objective validation (1–4,000 characters), reserved-word explicit set syntax, exact thread/profile binding, native status/usage/active-time visibility, and goal notifications.

Setting/resuming may immediately start billable model work and requires authorization. It shall use existing thread model/access settings, not silently apply pending preferences. Pause/clear affect future continuation and do not interrupt an in-flight turn. Changed objectives reset goal accounting; a same non-terminal objective or status-only update preserves it. Budget-limited goals require an explicit larger total or a new goal; no automatic increases. Switching threads shall not silently pause/clear the old goal. Preserve blockers for active turn, plan mode/open guided plan, approval, and pending input. Read back mutations; ambiguous results shall not be automatically retried. Missing native server support/flag shall be explicit; no synthetic prompt-loop substitute.

**Acceptance:** Exercise all goal operations, budgets, usage preservation/reset, blocked activation, old-thread persistence, native continuation, read-back ambiguity, pause-versus-stop distinction, and unsupported-server failure. Validate native behavior, not only parser tests.

### CP-20 — Attachments and staged media batches

**Source:** `src/controller/attachment_batch.ts`, `attachment_batch.test.ts`; `src/telegram/media.ts`; `src/store/attachment_state_repository.ts`.

Support staged files/media, grouped album batches, saved paths/metadata, next-message consumption, explicit use-next/analyze-now/clear, captions, and durable recovery. Preserve the reference handling of photos, documents, audio, voice, video, animation, stickers, and video notes through Hermes attachment facilities. Native image inputs and file-path context shall reflect actual Codex support; staging audio/video is not a claim of native transcription or video understanding.

**Acceptance:** Stage representative inputs of every supported attachment kind, merge an album, consume exactly once, clear another, queue attachment-bearing input, and recover pending batches without path/ownership leakage.

### CP-21 — Status, identity, usage, and limits

**Source:** `src/controller/status_command.ts`, `status.ts`, `status_preview.ts`; client account/rateLimits/read and account/rateLimits/updated.

Support `/status`: engine/instance, connected state, last error, runtime identity, provider/backend, account identity, current thread, configured/effective model/effort/variant/tier/mode/access, sync/plan/queue/history settings, active turns, pending approvals/input/attachments, queue depth, and rate-limit windows/reset information when exposed. Clearly label unavailable/stale snapshots; do not invent cost or quota data.

**Acceptance:** Compare status against real scoped state through connection changes, pending workflows, rate-limit updates, and account/provider switches without exposing unrelated sessions.

### CP-22 — Reconnect, restart, and health

**Source:** `src/controller/service_control.ts`, `service_control.test.ts`; `src/codex_app/client.ts`; `src/main.ts` diagnostics; platform service scripts.

Support `/reconnect`, authorized targeted restart, active-work maintenance guards, reconnect backoff, runtime reinitialization, and post-restart readiness/identity/limit refresh. Restart only the intended plugin-owned worker/service; restarting the entire Hermes gateway or another profile requires separate authorization. Preserve the user-visible requested-versus-ready distinction and completion notification.

**Acceptance:** Reconnect after a transport failure, block unsafe maintenance while work is active, restart the intended runtime, verify readiness by read-back, and confirm unrelated profiles/services remain intact.

### CP-23 — Persistence and recovery of all pending workflows

**Source:** `src/controller/controller.ts`, `recovery.test.ts`; `src/store/*_repository.ts`, `schema.ts`, `database.ts`.

Persist bindings, provider/settings state, queue, attachment batches, plans/history, unresolved approvals, structured inputs, preview metadata, audit events, and manager task records. Recover only still-valid runtime requests; visibly resolve stale ones rather than replay approvals or duplicate work. Live-turn recovery shall distinguish resumable, interrupted, and unknown states.

**Acceptance:** Restart at every pending workflow boundary, verify safe user recovery actions and correct ownership, and prove that duplicate messages/events cannot replay mutations.

### CP-24 — Scope, routing, and interaction safety

**Source:** `src/telegram/scope.ts`, `addressing.ts`, `gateway.ts`; controller ingress and profile routing.

Preserve authorized-user/chat/topic isolation as equivalent Hermes identity/session/channel authorization, targeted command handling, sticky thread binding, wrong-scope callback rejection, and stale action handling. Do not reintroduce a second Telegram gateway or assume every Hermes surface has Telegram identifiers.

**Acceptance:** Exercise private and topic/session-scoped interactions, wrong user/channel/profile, stale controls, and two simultaneous independent scopes with no data or action crossover.

### CP-25 — Presentation, localization, and actionable errors

**Source:** `src/i18n.ts`, `assistant_text.ts`; `src/controller/presentation.ts`, `turn_rendering.ts`, `telegram_message_service.ts`, `turn_completion.ts`.

Preserve understandable localized controls and equivalent progress/plan/approval/history/result presentation, safe formatting, long-message splitting, message-edit fallback, and actionable distinctions among auth, quota, rate limit, interruption, failure, and missing prerequisites. Use Hermes-native output rather than requiring identical Telegram markup or button layout. Retain reference language coverage where relevant to migrated controls; additional locales are separate scope.

**Acceptance:** Exercise existing locale variants, long and malformed output, removed/expired interactive messages, and each error class without losing result content or accidentally executing an action.

### CP-26 — Operational configuration and platform-aware behavior

**Source:** README Requirements/Service install; `src/config.ts`, `src/runtime.ts`, `src/platform/capabilities.ts`, `service_scripts.ts`, and `scripts/service/`.

Preserve Codex-related configuration outcomes, independent-instance behavior, diagnostics, secure credential-path resolution, and lifecycle support through Hermes-compatible installation/operation. Reference desktop/service behavior spans Linux, macOS, and Windows with platform-specific availability. Do not promise unsupported Hermes/SDK platforms; publish and qualify the compatibility matrix. Packaging need not duplicate the bridge's Node.js service or `.env` format. Unsupported target-platform dependencies remain explicit qualification gaps requiring user review, not silently removed requirements.

**Acceptance:** Validate configuration errors, profile isolation, install/enable/disable/uninstall instructions and lifecycle on each claimed supported platform; verify documented platform prerequisites and source conditional behaviors.

## Reference command coverage

Every key in `telegram_ingress.ts` commandHandlers is mapped below; aliases retain equivalent semantics:

- `start`, `help` → CP-01
- `status` → CP-21
- `login` → CP-12
- `where` → CP-13
- `threads` → CP-03
- `open`, `new` → CP-02
- `provider` → CP-11
- `model`, `models` → CP-09
- `tier`, `fast`, `effort` → CP-10
- `mode`, `plan` → CP-14
- `settings`, `permissions`, `access` → CP-13
- `reconnect`, `restart` → CP-22
- `queue`, `guide` → CP-08
- `goal` → CP-19
- `review` → CP-18
- `reveal`, `focus` → CP-05
- `stop`, `interrupt` → CP-07

Callback coverage additionally includes thread list/rename/archive, provider/settings toggles, plan confirmation/recovery, queued guidance, attachments, input editing/submission, and approval details/decisions under CP-03–04, CP-08–17, CP-20.

## Completion evidence contract

Before declaring feature parity, maintain an implementation evidence record for every CP entry with: source behavior/subcase, pinned SDK method or approved adaptation, Hermes action, configuration dependencies, automated tests, real integration evidence, platform coverage, and unresolved gaps. Expand each grouped entry into individual action/negative/recovery cases during test planning.

All required entries must pass their applicable acceptance cases. `blocked`, `not implemented`, `mock-only`, and `not tested` are not `pass`. Runtime prerequisite rejection is a negative test, not proof that the positive feature works. Any exclusion, phase deferral, or behavior reduction requires explicit user approval.

This catalogue includes source-discovered features beyond README. A final command/callback/provider-method/regression-test reconciliation against the recorded baseline remains a mandatory implementation gate; it is not claimed to have been executed as a runtime parity test during SRS drafting.
