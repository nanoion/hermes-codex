# Software Requirements Specification — hermes-codex

- **Document version:** 0.4
- **Status:** Draft for stakeholder review; not implementation approval
- **Project:** `/projects/hermes-codex`
- **Product:** Hermes plugin integrating Codex through the Python SDK

## 1. Purpose and business outcome

Enable Hermes to manage software-engineering work while Codex executes scoped tasks as a worker. The user interacts with Hermes; Hermes defines the assignment, monitors execution, handles decisions, verifies outputs, and communicates the result.

The integration must preserve accountability: a successful Codex turn is not automatically a successfully delivered user requirement.

### 1.1 Confirmed user requirements

- **UR-01:** Deliver a Hermes plugin connecting to Codex through a Python SDK.
- **UR-02:** Use `/projects/telegram-codex-app-bridge` as an architectural reference; its ideas may be reused.
- **UR-03:** Codex is the worker; Hermes is the manager.
- **UR-04:** Support all Codex features present in the current working tree of `/projects/telegram-codex-app-bridge`, including shared bridge features used by Codex. Full behavioral parity is required; SDK gaps do not authorize feature removal.

### 1.2 Requirement status and terminology

UR-01–UR-04 are explicitly confirmed by the user. Full Codex feature coverage is confirmed scope. Detailed implementation choices and manager-specific extensions below remain proposed derivations for review. The normative parity catalogue in Section 15 and `CODEX_FEATURE_PARITY.md` defines the baseline coverage. Within this draft, “shall” describes a candidate acceptance requirement if the SRS is approved. “Should” identifies a recommendation.

This document specifies the product, not permission to invoke Codex, install dependencies, modify Hermes configuration, commit, push, or deploy during this documentation task.

## 2. Scope

### 2.1 Required feature coverage and proposed delivery mechanics

- Python-based Hermes plugin and Codex SDK adapter.
- Structured assignment of work in an authorized local project workspace.
- Codex thread creation and continued work on an existing task.
- Non-blocking task execution, status, progress, results, and cancellation.
- Approval escalation, permission boundaries, and scoped session ownership.
- Durable task records and safe recovery after interruption.
- Evidence handoff to Hermes for independent verification.
- Configuration, diagnostics, tests, and operator documentation.
- Full reference Codex behavior: thread management, desktop reveal/sync, native review, native goals, provider/account switching, model/effort/tier settings, planning, structured input, queue/steering, attachments, status, and recovery. See the normative parity catalogue.
- Delivery may be incremental, but a partial increment shall not be labeled feature-complete.

### 2.2 Not included unless separately approved

- A second Telegram bot or replacement Hermes messaging gateway.
- Gemini or other worker engines.
- A new custom web UI; existing Codex desktop reveal/focus and synchronization behavior remains IN scope.
- Remote execution clusters, distributed scheduling, or automatic multi-worker fan-out.
- Automatic Git commit, push, merge, deployment, or destructive operations.
- Modification of Hermes core or migration of existing bridge databases and credentials.
- Guaranteed success or an invented unlimited manager retry loop. Native Codex Goals and their server-controlled continuation/budget semantics remain IN scope.

## 3. Actors and responsibilities

### User

Supplies goals, approves scope and sensitive actions, and resolves business decisions.

### Hermes — manager

- Understands the goal and determines whether delegation is appropriate.
- Creates a self-contained task brief with scope and acceptance criteria.
- Obtains authorization where required before dispatch.
- Monitors the worker and routes clarification or approval requests.
- Independently checks artifacts and evidence, accepts or rejects the output, and requests bounded corrections when authorized.
- Owns all user-facing completion claims.

### Codex — worker

- Executes only the assigned task within effective workspace and permission limits.
- Reads relevant repository instructions and preserves unrelated user changes.
- Produces requested artifacts, test evidence, and a concise handoff.
- Reports blockers and approval needs rather than expanding scope.
- Does not approve its own output or grant itself additional permissions.

### Plugin — orchestration boundary

Connects the two runtimes, validates requests, maintains lifecycle state, enforces available technical controls, and translates SDK events into manager-readable records. It is not a replacement for Hermes reasoning or an independent product manager.

## 4. Reference findings and technical constraints

### 4.1 Existing bridge

Read-only inspection of the reference project established:

- `README.md` describes a Node.js/TypeScript bridge to `codex app-server`, with thread binding, queued follow-ups, guided plan approval, interrupt, reconnect, and restart recovery.
- `src/engine/codex_provider.ts` separates engine operations from the messaging layer and exposes thread start/resume, turn start/steer/interrupt, event forwarding, approval responses, and profile-aware client selection.

These are required behavioral reference points under UR-04, not evidence that the Python SDK supports every bridge feature. The new plugin shall reuse concepts rather than require the bridge service or port its Telegram-specific implementation wholesale. Source-code copying requires license and attribution review first; that review has not been performed.

### 4.2 Hermes integration

Official Hermes documentation describes plugins using `plugin.yaml`, Python `register(ctx)`, and `ctx.register_tool(...)`, without modifying core code. Installation and state shall resolve against the active Hermes profile rather than hardcoding the default profile.

The installed Hermes version's lifecycle, asynchronous execution, and notification interfaces must be verified before implementation. This SRS does not assume an undocumented background callback API.

### 4.3 Codex Python SDK

The retrieved official SDK documentation identifies:

- Package: `openai-codex`.
- Python import namespace: `openai_codex`.
- Synchronous `Codex` and asynchronous `AsyncCodex` clients.
- A local Codex app-server accessed through JSON-RPC.
- Python 3.10 or later and a pinned CLI runtime dependency in published SDK builds.

Documentation/search results contain older SDK names and inconsistent maturity descriptions. Therefore the exact SDK version, supported host architecture, API signatures, authentication behavior, approval callbacks, progress streaming, interrupt, and resume behavior must be qualified against a pinned release before implementation. No particular release or model is selected by this SRS.

Using an SDK-managed app-server process is compatible with the Python SDK requirement. Replacing it with a handwritten shell-prompt wrapper or direct raw protocol client is not an implicit fallback.

## 5. Primary use cases

### UC-01 — Delegate a scoped engineering task

1. User requests work through normal Hermes conversation.
2. Hermes discovers relevant scoped Codex work or defines scope, constraints, permissions, and acceptance criteria.
3. For write-capable work, Hermes prepares an exact proposal and presents one native Action First choice prompt: Approve and start, Cancel, or View details. Slash commands remain a fallback, not the routine path.
4. Plugin validates the workspace, ownership, configuration, and readiness.
5. Plugin durably records the assignment and returns a task identifier without requiring the user to manage it.
6. Codex executes; Hermes remains available for conversation and control.
7. Hermes follows bounded status/event/result pagination and summarizes the evidence.
8. Hermes verifies actual artifacts and reports accepted work, required corrections, or a blocker.

### UC-02 — Continue or correct work

Hermes submits a follow-up tied to the existing task and Codex thread. The plugin preserves ownership and permission boundaries. A follow-up shall not silently start an unrelated thread when resume fails.

### UC-03 — Handle a decision or approval

The worker requests information or permission. Hermes presents the exact action and relevant scope through native `clarify` choices when the decision is bounded; free-text questions remain normal replies. Only a valid, current decision may be relayed. Denial, cancellation, expiry, replay, unsupported approval mediation, or ambiguous identity shall not result in automatic approval.

### UC-04 — Inspect and cancel

Hermes retrieves task state and recent events. On an authorized cancellation, the plugin requests interruption and distinguishes “cancellation requested” from “worker stopped.” Files already changed are not automatically reverted.

### UC-05 — Recover after a restart

The plugin loads persisted task identifiers and reconciles them with the SDK/runtime when supported. If the real execution state cannot be established, it reports an unknown/interrupted state and does not blindly rerun work.

## 6. Functional requirements

### FR-01 — Plugin packaging and profile isolation

The system shall register manager-facing tools through supported Hermes plugin interfaces, without patching Hermes core. Configuration, task records, and logs shall be isolated to the active profile. Disabling the plugin shall not break unrelated Hermes tools.

### FR-02 — Readiness and capability discovery

Before accepting execution, the system shall validate SDK/runtime availability, authentication readiness, selected model/configuration, and workspace access. It shall expose a capability summary and actionable errors without revealing credentials. Unsupported required safety capabilities shall block execution rather than weaken policy.

### FR-03 — Structured assignment

Each assignment shall include:

- Objective and task type, such as inspect, implement, test, or review.
- Canonical project/workspace path and allowed write scope.
- Relevant context and repository instruction locations.
- Explicit in-scope and out-of-scope work.
- Acceptance criteria and requested verification commands or evidence.
- Effective permission policy and authorization reference where applicable.
- Execution timeout and an idempotency key.

Missing mandatory fields shall produce validation errors before worker execution. Secrets and unrelated conversation history shall not be included automatically.

### FR-04 — Task and thread ownership

The plugin shall bind every task to a canonical scope derived from the active Hermes profile, platform, trusted user, chat, conversation type, and thread identity, plus workspace, Codex thread, and turn identifiers. A slash command and a later model-facing tool call in the same trusted conversation shall resolve to that same canonical scope even when their transient Hermes session IDs differ. Different profiles, platforms, users, chats, and threads shall remain isolated; group/channel contexts shall not inherit DM ownership. These identities shall come from trusted runtime context, not worker text or caller-supplied ownership claims. Access outside the authorized scope shall be denied. Legacy session-bound records may be migrated only through an exact derived alias, atomically and without relocating or losing their credential, worker-thread, or attachment storage.

Routine use shall support natural conversation and context-sensitive `/codex` controls. Task/proposal IDs may be omitted only when the eligible target is unique; mutating commands shall present a choice instead of guessing when multiple targets remain. When the current conversation is interpreted as the user's request or confirmation, the model-facing tool may invoke all compatibility user controls—including authorization and permission decisions—through a strictly validated `user-control` operation. `/codex` and `/codex-user` remain manual alternatives. This is an explicit product decision to trust model interpretation rather than require a slash-only authorization boundary; trusted owner derivation and downstream policy checks remain mandatory.

### FR-05 — Non-blocking execution

Dispatch shall return a durable task identifier without waiting for the entire worker turn. Hermes shall be able to inspect or cancel the task while it runs. The execution host/lifecycle mechanism remains an implementation decision and must be demonstrated against the target Hermes runtime.

### FR-06 — Progress and event reporting

The plugin shall normalize available SDK events into timestamped records with task ID, event type, sequence/cursor, and sanitized payload. It shall expose lifecycle changes, worker messages, approval needs, and errors. It shall not fabricate percentages or infer successful completion from a text message. Updates shall be bounded and coalesced to avoid flooding the user or manager context.

### FR-07 — Follow-up and concurrency control

The system shall support continued work on an existing task/thread. Proposed default: one active turn per task and one writing task per canonical workspace. Conflicting writes shall not execute concurrently by accident. Follow-ups shall preserve the reference durable queue, auto-queue toggle, cancel-next/clear operations, and explicit steer-now versus keep-queued choice. Mid-turn steering is REQUIRED under UR-04. An SDK gap shall be reported as a release-blocking parity gap, not accepted as feature completion.

### FR-08 — Approval mediation

Approval requests shall be persisted with request ID, proposed action, scope, task/turn binding, expiry, and decision state. The plugin shall expose owner-scoped, non-authoritative decision descriptors so Hermes can render native `clarify` choices; callback labels themselves shall not grant authority. Proposal confirmation shall use Action First ordering: Approve and start, Cancel, View details. Permission, plan, and bounded structured-input choices shall use the same pattern; free-text input shall remain a normal reply. Decisions shall be checked against the trusted runtime-derived owner and the current conversation's interpreted request or confirmation, whether invoked through `user-control`, `/codex`, or `/codex-user`. Duplicate, expired, cancelled, cross-task, cross-owner, or stale approvals shall be rejected. The plugin shall not enable blanket automatic approval merely to bypass an SDK limitation.

### FR-09 — Permission enforcement

Assignments shall use explicit sandbox and approval settings. Read-only work shall use enforceable read-only execution. Write access shall be limited to approved roots, with canonical-path and symlink escape checks. Network and external side effects shall follow explicit policy. Unsupported restrictions shall fail closed.

A prompt saying “do not commit or deploy” is not a security boundary. The implementation must document which restrictions are enforced by the runtime, environment isolation, credentials, or approval controls. High-risk actions shall remain unavailable when the required enforcement cannot be demonstrated.

### FR-10 — Worker result and evidence

A result shall include execution outcome, summary, changed artifact paths, reported commands and exit statuses when available, verification results, unresolved issues, and SDK thread/turn references. Reported evidence and independently verified evidence shall be labeled separately. A completed SDK turn shall become ready for manager review, not automatically accepted.

### FR-11 — Manager review and bounded rework

Hermes shall be able to record acceptance, rejection, or a request for corrections with evidence references. Corrections shall be new auditable turns under the same task or a linked task. Proposed default: no automatic manager rework without a manager decision; no unbounded manager retry loop. An explicitly authorized native Codex Goal may continue under its existing thread policy and goal budget; this does not grant permission for Hermes to fabricate a retry loop or increase the budget.

### FR-12 — Cancellation and timeout

Cancellation shall persist intent before requesting interruption. A task shall be marked cancelled only after stop confirmation. Timeouts shall trigger a stop/reconciliation path rather than imply that the worker stopped. Unconfirmed termination shall retain a visible unknown/recovery-required outcome. Cancellation shall not claim rollback.

### FR-13 — Durable recovery and idempotency

Task creation, transitions, execution identifiers, approval decisions, and result metadata shall survive plugin restart. Repeated dispatch with the same ownership scope and idempotency key shall return the original task; a conflicting payload shall be rejected. A crash during SDK submission shall be treated as an uncertain submission until reconciled, not automatically resubmitted.

### FR-14 — Failure handling

Errors shall distinguish validation, authentication, authorization, capability mismatch, workspace conflict, rate limit, transport failure, worker failure, timeout, cancellation failure, and unknown execution outcome. Retryable read operations may use bounded backoff. Mutating operations shall not be retried without reconciliation or supported idempotency guarantees.

### FR-15 — Configuration and diagnostics

The system shall support validated configuration for allowed workspace roots, worker model, SDK/runtime selection, timeouts, concurrency limits, output limits, and retention. Secrets shall use protected credential mechanisms, not committed configuration or task payloads. Diagnostics shall report sanitized versions, capability support, and readiness; they shall not silently run billable work.

### FR-16 — Complete reference feature parity

The plugin shall implement every CP requirement in `CODEX_FEATURE_PARITY.md`. That file is a normative part of this SRS. Hermes tools and interactions may replace Telegram command syntax/cards, but all actions, decision paths, settings, persisted state, safety guards, and user-observable outcomes shall remain available. Neither runtime capability errors nor manual workarounds count as a delivered required feature. Platform/environment prerequisites may constrain where a feature runs, but shall not remove it from product scope.

## 7. Logical manager-facing interface

The following names are proposed plugin contracts, not claims about existing Hermes or SDK methods:

- `codex_worker_health`: readiness, versions, and supported capabilities.
- `codex_worker_submit`: validated assignment → durable task ID and initial state.
- `codex_worker_status`: current execution/review state and recent activity.
- `codex_worker_events`: cursor-based, bounded event retrieval.
- `codex_worker_continue`: authorized follow-up on the existing task.
- `codex_worker_cancel`: cancellation request and acknowledgement state.
- `codex_worker_result`: worker result plus evidence references.
- `codex_worker_approve`: resolve a current approval request using trusted authorization.
- `codex_worker_review`: record Hermes acceptance or required corrections.

The tool surface shall additionally expose thread list/search/read/open/rename/archive/unarchive/reveal; model/provider/account selection; tier/effort/mode/access settings; native review; native goal get/set/pause/resume/clear; queue list/cancel-next/clear/steer; attachment stage/consume/clear; plan confirm/revise/cancel/recover; structured input reply/edit/cancel; and reconnect/restart. These may be grouped into validated action-based tools rather than one tool per action. Native Codex review is distinct from `codex_worker_review`, which records manager acceptance.

All responses shall provide a stable success/error envelope, task identifier when applicable, machine-readable state/error code, and a concise manager-readable message. Tool schemas shall reject invalid parameters and shall not accept arbitrary shell command templates for launching the SDK.

## 8. Lifecycle and data requirements

### 8.1 Execution lifecycle

Proposed states:

`pending → starting → running → completed | failed`

Additional transitions:

- `running → awaiting_approval → running | failed`
- `pending → cancelled`
- `starting | running | awaiting_approval → cancelling → cancelled`
- Active states may become `unknown` when execution status cannot be established.
- Timeout is recorded as a reason and enters cancellation/reconciliation; it is not proof of termination.
- Recovery may resolve `unknown` to a proven running or terminal state.

A follow-up creates a new attempt/turn record rather than rewriting the history of a completed turn.

### 8.2 Review lifecycle

Review state is separate from execution state:

`not_ready → pending_review → accepted | changes_requested | rejected`

Only Hermes' authorized review action may record acceptance. A task with worker errors may still be reviewed for partial artifacts but shall not be represented as a successfully completed assignment.

### 8.3 Minimum persisted entities

- **Task:** ID, trusted owner scope, workspace, brief, criteria, effective policy, idempotency key, timestamps.
- **Attempt/turn:** task ID, thread ID, turn ID, execution state, cancellation intent, timeout/failure reason.
- **Event:** task/attempt ID, ordered cursor, event type, timestamp, sanitized bounded payload.
- **Approval:** task/turn/request binding, action, scope, expiry, trusted decision evidence.
- **Result:** summary, artifact references, reported command/test outcomes, unresolved issues.
- **Review:** reviewer context, decision, acceptance-criterion evidence, linked correction attempt.

A local transactional store is recommended; SQLite is a candidate, not an approved implementation choice. Full source files and raw transcripts shall not be persisted by default. Retention shall not delete active tasks or unresolved approval/recovery records.

## 9. Non-functional requirements

- **NFR-01 — Security:** Do not expose secrets in prompts, tool responses, logs, or exception traces. Treat repository content and worker output as untrusted data, not authorization.
- **NFR-02 — Responsiveness:** Proposed target: local status/cancel-request acknowledgement p95 within two seconds for a healthy local store. This excludes SDK startup, model latency, and actual stop completion.
- **NFR-03 — Reliability:** Replayed events and repeated requests shall not duplicate work or overwrite terminal outcomes. State transitions shall be transactional and auditable.
- **NFR-04 — Resource bounds:** Concurrency, event size, stored output, timeouts, and retention shall be configurable and finite. Unknown token/cost usage shall remain unknown, not zero.
- **NFR-05 — Compatibility:** Pin and test a specific Hermes, Python, SDK, and runtime combination. Initial OS/architecture support is pending confirmation; do not claim all-platform support without execution evidence.
- **NFR-06 — Maintainability:** Separate Hermes registration, orchestration/state, SDK adaptation, and policy validation. Avoid dependencies on the reference bridge's runtime.
- **NFR-07 — Observability:** Correlate sanitized diagnostics by task/thread/turn; distinguish received, submitted, running, completed, and manager-accepted milestones.
- **NFR-08 — Honest verification:** Offline mocks and live SDK tests shall be reported separately. A mocked adapter test is not proof of real Codex integration.

## 10. Acceptance criteria and traceability

All scenarios below are proposed release gates once approved.

- **AC-01 — Plugin load (UR-01, FR-01):** Enable the plugin in a test Hermes profile; tools register through supported APIs without core modifications, and another profile remains unaffected.
- **AC-02 — Real SDK path (UR-01, FR-02):** With explicitly authorized credentials and a pinned SDK/runtime, perform a harmless task through the Python SDK and retrieve its actual output. Report package/runtime versions and identifiers without secrets.
- **AC-03 — Manager/worker handoff (UR-03, FR-03–06, FR-10–11):** Submit a task with criteria, receive an ID before completion, retrieve progress and artifacts, then demonstrate separate manager review. Worker completion alone cannot set accepted status.
- **AC-04 — Continuation (UR-02–03, FR-04, FR-07):** Continue work using the same bound thread and preserved scope; an invalid thread returns an error rather than silently resetting context.
- **AC-05 — Approval safety (UR-03, FR-08–09):** A restricted action is blocked or awaits authorized approval. Denied, expired, cross-session, and replayed decisions never authorize it.
- **AC-06 — Workspace isolation (FR-04, FR-09):** Cross-profile task access, path traversal, symlink escapes, and writes outside allowed roots are rejected by validated technical controls.
- **AC-07 — Cancellation (FR-12):** Test pending and running cancellation, including an unresponsive runtime. A requested but unconfirmed interruption is never reported as stopped or rolled back.
- **AC-08 — Recovery/idempotency (UR-02, FR-13):** Restart during execution and during submission, replay a dispatch key, and verify durable recovery or an explicit unknown state without blind duplicate execution.
- **AC-09 — Concurrency (FR-07):** Concurrent writing assignments to the same workspace cannot execute simultaneously under the proposed default policy.
- **AC-10 — Failures and redaction (FR-02, FR-14–15, NFR-01):** Missing auth, incompatible runtime, rate limits, invalid model, oversized output, and transport loss produce actionable sanitized errors.
- **AC-11 — Responsiveness (FR-05, NFR-02):** During a long worker turn, manager status and cancellation remain usable; measure the proposed local response target with the test environment recorded.
- **AC-12 — Evidence integrity (FR-10–11, NFR-08):** A worker claiming tests passed without evidence cannot produce verified/accepted status automatically. Hermes verifies real artifacts and marks each criterion passed, failed, or unverified.

- **AC-13 — Full reference parity (UR-04, FR-16):** Execute every CP acceptance scenario against the pinned baseline, record implementation and live-integration evidence separately, and confirm every source command, callback, public Codex operation, and applicable regression case is mapped. No missing/blocked requirement may be reported as full parity; any exclusion or deferral requires explicit user approval.

Traceability summary:

- UR-01 → FR-01–02, FR-05, FR-15 → AC-01–02, AC-10–11.
- UR-02 → FR-04, FR-06–08, FR-12–13 → AC-04–05, AC-07–08.
- UR-03 → FR-03–14 → AC-03–09, AC-12.
- UR-04 → FR-16 and CP-01–CP-26 in `CODEX_FEATURE_PARITY.md` → AC-13 plus each catalogue acceptance scenario.

## 11. Risks and required mitigations

- **SDK feature mismatch:** Qualify the pinned API before coding; unsupported must-have capabilities are blockers, not hidden scope reductions.
- **Hermes lifecycle mismatch:** Verify a supported non-blocking execution and event-delivery mechanism; do not invent a callback interface.
- **Permission mismatch:** SDK sandbox settings may not enforce every business restriction. Demonstrate controls and fail closed where they cannot.
- **Crash ambiguity:** Persist submission intent and reconcile identifiers; never claim exactly-once remote execution without supporting guarantees.
- **Conflicting workspace writes:** Enforce workspace locking and preserve pre-existing changes.
- **Auth and cost exposure:** Keep credential handling outside task text; separate diagnostics from authorized billable integration tests.
- **Untrusted worker output:** Validate structured fields and artifact paths; never treat worker messages as user approval.
- **Reference-project coupling:** Reuse architecture ideas without importing Telegram deployment assumptions or private data.

## 12. Open decisions before implementation

1. **SDK baseline:** Which exact `openai-codex` release and runtime can satisfy the entire parity catalogue? If the SDK lacks a required method, qualify an SDK-supported extension or request an explicit architecture decision; do not reduce feature scope.
2. **Authentication:** Reuse an existing Codex login or configure a dedicated worker identity? Which supported auth mode is permitted?
3. **Execution scope:** Recommended first release is a local worker on the Hermes host. Is remote execution required?
4. **Autonomy:** Recommended default is approved-scope execution with Hermes review and explicit sensitive-action escalation. Which actions may be pre-authorized?
5. **Concurrency:** Recommended first release allows one writing task per workspace. Are isolated parallel worktrees required initially?
6. **Completion delivery:** Which supported Hermes notification/lifecycle facility will deliver progress, decisions, and terminal events? Proactive delivery is required for behavioral parity; its integration mechanism needs runtime qualification.
7. **Model and limits:** Which worker model, execution timeout, retry/rework budget, and spending policy should apply?
8. **Persistence and retention:** Is a profile-local SQLite store acceptable, and how long should sanitized completed-task data be retained?
9. **Distribution:** Local trusted plugin or installable repository/package? Which OS/architectures must be supported?

These decisions need not prevent reviewing this SRS, but implementation must not silently resolve material scope or security choices.

## 13. Delivery and verification expectations

If implementation is later approved, delivery shall include plugin source, dependency pins, configuration documentation, manager workflow guidance, automated tests, authorized real-SDK parity tests, and an evidence-backed verification report covering every catalogue entry. A smoke test alone is insufficient for UR-04.

No implementation, plugin installation, credential changes, worker invocation, Git operation, or deployment is performed by creating this SRS.

## 14. Sources and evidence boundaries

1. User requirements in this conversation: Python SDK integration, reference bridge, Hermes manager/Codex worker relationship.
2. Local `/projects/telegram-codex-app-bridge/README.md`, particularly runtime, capabilities, and prerequisite sections.
3. Local `/projects/telegram-codex-app-bridge/src/engine/codex_provider.ts`, inspected provider capabilities and lifecycle methods.
4. Hermes official plugin documentation: https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
5. OpenAI official Codex SDK documentation: https://developers.openai.com/codex/codex-sdk

Documentation was retrieved while preparing this draft. The SDK package was not installed or exercised, and the reference bridge was not run. Retrieved documentation was sufficient to establish the integration direction, not to certify all proposed capabilities or exact SDK compatibility.

## 15. Full Codex parity baseline (UR-04)

The normative companion [CODEX_FEATURE_PARITY.md](CODEX_FEATURE_PARITY.md) specifies CP-01–CP-26, source locations, behavior, command/callback coverage, and acceptance scenarios. Full coverage is confirmed scope, not an optional later phase. Its requirements take precedence over any earlier draft language suggesting a reduced feature subset.

The parity catalogue records the inspected source locations and behavioral expectations without publishing credentials, runtime databases, local paths, or machine-specific evidence.

Additional persisted entities shall cover native goals and usage/status references, thread-list/filter context, provider/account associations, settings, queue entries, attachment batches, guided plans/history, structured input, and recovery metadata. Native goal lifecycle and structured-input/plan waiting states shall remain distinct from execution and manager-review state.

The Python SDK requirement remains unchanged. Missing SDK methods require compatibility work or an explicitly approved architecture decision, never silent feature cuts. Native Goals, code review, desktop reveal/sync, account/provider switching, and active steering are all required. Existing desktop/platform prerequisites shall be documented and tested on suitable hosts.

### Revision history

- **0.1:** Initial manager/worker SRS, with proposed limited initial feature scope.
- **0.2:** User confirmed full Codex reference feature coverage. Added UR-04, FR-16, AC-13, normative parity catalogue, and source baseline manifest. Removed desktop-reveal exclusion and optional steering; added native Goals, native review, accounts/providers, complete workflows, and full parity release criteria. Implementation is covered by the repository test suite.
