# AGENTS.md

## Purpose and source routing

This repository contains Marketing Intelligence, a local-first web application for a non-technical Russian-speaking internet marketer.

The first development version is local, but the required production target is a multi-user deployment on the user's corporate server. Preserve that future deployment path in current designs without introducing server infrastructure before its approved stage; see DEC-022.

Always read:

- this file;
- the current task in `TASKS.md`;
- only the requirement sections referenced by that task in `REQUIREMENTS.md`.

Read other documents only when relevant:

- `PROJECT.md` and `DECISIONS.md` — architecture, dependencies, product boundaries, or a new decision;
- `ROADMAP.md` — scope, sequencing, or milestone changes;
- `UX_UI_PRINCIPLES.md` — interface or user-flow changes;
- `README.md` and `docs/` — installation, operation, or user instructions;
- `CHANGELOG.md` and `PROJECT_LOG.md` — completion records, not general implementation context.

Do not reread unrelated unchanged documents. If sources conflict, stop and report the conflict.

## Product constraints

- Keep the current application local-first and useful without paid services, mandatory public cloud hosting, paid proxies, or paid AI APIs.
- Treat Windows 10/11, macOS, and Linux as equal local desktop targets. Keep the same product capabilities available on each platform; only launch and environment commands may differ.
- Preserve the mandatory path to a multi-user corporate-server deployment; do not add PostgreSQL, authentication infrastructure, distributed workers, or deployment components before the stage that needs them.
- Treat every value received from a browser, import, integration, network response, background job, command, or configuration source as untrusted. Validation, access decisions, and data-integrity rules belong on the server and must not rely on frontend controls.
- Free external services may be optional integrations only.
- Prefer simple, maintainable, open-source, locally installable solutions with suitable licenses.
- Use a modular monolith; do not add microservices, Redis, Celery, Elasticsearch, Kubernetes, or similar infrastructure without a measured need and recorded decision.
- Do not replace the approved stack, expand task scope, or implement speculative features silently.
- Never claim that a test or command succeeded unless it was actually run.
- Never commit credentials, generated databases, browser profiles, logs, environments, or large crawl archives.

Approved stack for the current local version: Python, FastAPI, SQLite, SQLModel, server-rendered HTML or minimal JavaScript, HTTPX, BeautifulSoup, lxml, APScheduler, Pytest, and Git. Use Playwright only when ordinary HTTP retrieval is demonstrably insufficient. PostgreSQL becomes mandatory at the approved corporate-server stage; pure SQLAlchemy remains conditional on a separate measured need. Add dependencies only for a current requirement when no simpler option is adequate.

## Architecture and implementation

- Separate domain logic from HTTP routes and interface code.
- Keep crawling, parsing, comparison, persistence, and presentation as distinct modules with clear interfaces.
- Avoid global mutable state, duplicate implementations, premature abstractions, and dead code.
- Keep configuration outside source code and secrets in ignored environment or local configuration.
- Avoid hard-coded machine paths and Windows-only assumptions; keep runtime paths, database URLs, hosts, ports, and other deployment settings configurable.
- Use cross-platform Python and web standards for paths, environment variables, text encoding, filesystem operations, processes, and browser interfaces. If a platform was not actually tested, document that limitation instead of claiming verified support.
- Keep durable data and long-running-operation state behind explicit persistence interfaces; do not introduce new correctness-critical state that exists only in one process memory.
- Isolate database-engine, session, transaction, locking, and filesystem details so future PostgreSQL and corporate-server migration do not require changes to crawling, parsing, comparison, or presentation logic.
- Keep request validation, object access, state-changing operations, and outbound network policy behind explicit application boundaries that can later receive authenticated user, role, and tenant context without rewriting domain logic.
- Make database changes explicit and reversible; preserve previously collected data.
- Make the smallest coherent change that completes the current task and preserve unrelated behavior.
- Add or update relevant tests and handle expected errors.
- Write user-facing text in clear Russian.

For interface work, follow `UX_UI_PRINCIPLES.md`. Keep one clear primary action per screen, use progressive disclosure for advanced actions, preserve accessibility and visible feedback, and require confirmation for dangerous actions.

## Safety and integrity

For crawling: respect `robots.txt`; use a descriptive User-Agent, delays, limits, and timeouts; normalize URLs; avoid destructive links, infinite navigation traps, repeated variants, and automatic form submission; never bypass authentication, CAPTCHAs, access controls, or anti-bot protections.

For data: use transactions for multi-step writes; do not delete history without explicit confirmation; keep ordinary file-copy backup possible; a failed crawl must not corrupt earlier results; record status, timestamps, errors, and partial completion; distinguish missing data from zero.

For application security: reject malformed or excessive input at the server boundary; use allowlists and parameterized data access; encode untrusted output; protect state-changing browser requests; restrict redirects and outbound requests; do not expose secrets, stack traces, internal paths, or unrelated data in responses and logs. Security controls must remain portable to PostgreSQL and multi-user deployment. Actual authentication, corporate roles, TLS termination, network perimeter, and PostgreSQL privileges are implemented and rechecked at the approved corporate-server stage.

Label analytical output as a fact, calculated metric, correlation, hypothesis, or recommendation. Never present a hypothesis as a confirmed cause.

## Workflow

Before editing:

1. Inspect the current task, relevant requirements, existing implementation, and affected files.
2. Check scope, dependencies, data risk, and whether a decision is required.
3. Restate the result only when ambiguity must be resolved.

During and after editing:

1. Implement only the current task.
2. Run the narrowest relevant tests and a real verification command where practical.
3. Report commands and results honestly.
4. Update only affected documentation.
5. After successful completion, update `TASKS.md` and `PROJECT_LOG.md`; update `README.md`, `ROADMAP.md`, `CHANGELOG.md`, `DECISIONS.md`, or `PROJECT.md` only when their subject actually changed.

Tasks must follow the approved order. Do not begin the next task before the current one is implemented, verified, documented, and accepted. Record blockers in `TASKS.md` rather than skipping ahead.

## Token-efficient operation

- Do not repeat the prompt, unchanged project context, or successful logs.
- Use targeted file reads and searches. Prefer `rg`, specific paths, line ranges, `pytest -q`, `--maxfail=1`, and compact Git summaries.
- Preserve complete commands, relevant error text, acceptance criteria, risks, and blockers; brevity must not remove decision-critical information.
- Intermediate updates should normally be at most two short sentences.
- The final report should contain only: result, changed files, verification, limitations, commit/PR, and one next step. Keep it within 12 bullets unless a failure requires detail.
- Do not use subagents or broad web research for a well-scoped mechanical task unless they materially reduce risk or time.

## Definition of done and stop conditions

A task is complete only when the requested flow works end to end, relevant tests pass, expected errors are handled, documentation is current, verification is understandable in Russian, and no mandatory paid dependency was introduced.

Stop and explain before proceeding when credentials are unavailable, an action may destroy data, a mandatory paid dependency would be required, project sources conflict, or tests expose data-loss or security risk. Difficulty alone is not a stop condition: complete the safe verified portion and report the limitation.
