# AGENTS.md

## Project identity

This repository contains Marketing Intelligence, a local-first web application for an internet marketer.

Read these files before making changes:

1. `PROJECT.md`
2. `REQUIREMENTS.md`
3. `ROADMAP.md`
4. `DECISIONS.md`
5. `TASKS.md`
6. `UX_UI_PRINCIPLES.md`
7. `README.md`

Treat them as the project's source of truth.

## User profile

The product owner is not a programmer.

All summaries, instructions, errors, verification steps, and handoff notes must be understandable to a non-technical Russian-speaking user.

Use technical terminology only when necessary and explain it in plain Russian.

## Core constraints

1. The application must be local-first.
2. Core functionality must work without paid services.
3. Do not introduce mandatory paid APIs.
4. Do not introduce mandatory cloud hosting.
5. Do not introduce paid proxies.
6. Do not use trial services as critical infrastructure.
7. Do not use the OpenAI API or another paid AI API as a core dependency.
8. Free external services may only be optional integrations.
9. Prefer open-source, locally installable dependencies with suitable licenses.
10. Prefer the simplest maintainable implementation.
11. Do not introduce microservices in the initial versions.
12. Do not introduce Redis, Celery, Elasticsearch, Kubernetes, or similar infrastructure unless an existing measured problem requires it.
13. Do not replace the approved technology stack without documenting the decision.
14. Do not silently expand the scope of a task.
15. Do not fabricate successful test results or claim that a command was run when it was not.

## Initial approved stack

- Python
- FastAPI
- SQLite
- SQLModel
- server-rendered HTML or minimal JavaScript
- HTTPX
- BeautifulSoup and lxml
- Playwright only where normal HTTP retrieval is insufficient
- APScheduler
- Pytest
- Git

Use additional dependencies only when they solve a current requirement and no simpler solution is adequate.

## Architecture rules

- Begin with a modular monolith.
- Keep domain logic separate from HTTP routes and interface code.
- Keep crawling, parsing, comparison, persistence, and presentation as distinct modules.
- Use clear interfaces between modules.
- Avoid global mutable state.
- Keep configuration outside source code.
- Store secrets only in environment variables or ignored local configuration.
- Make database changes explicit and reversible.
- Preserve previously collected crawl data.
- Avoid premature abstractions.
- Avoid speculative features.

## UX/UI rules

- Read `UX_UI_PRINCIPLES.md` before changing interface code.
- Basic actions must be visible and understandable immediately.
- Advanced actions must use progressive disclosure.
- Do not display all possible actions at the same time.
- Do not create separate beginner and expert modes unless explicitly approved.
- Do not hide essential actions inside menus.
- Do not add several visually equal primary buttons to one screen.
- Prefer one clear primary action per screen.
- Advanced actions should be integrated into existing contextual controls where appropriate.
- Split buttons may visually appear as one element, but their click areas must remain behaviorally distinct.
- Minimalism must not reduce accessibility or feedback.
- Keyboard navigation, visible focus, and understandable labels are required.
- Dangerous actions must never be the default action.
- Reuse existing interaction patterns instead of inventing a new one for each screen.

## Development workflow

Before changing code:

1. Read the relevant project documentation.
2. Inspect the existing implementation.
3. Restate the requested result.
4. Identify affected files.
5. Check whether the task introduces a paid or external dependency.
6. Prefer modifying existing code over creating a parallel implementation.

During implementation:

1. Make the smallest coherent change that completes the task.
2. Preserve existing behavior unless the task explicitly changes it.
3. Add or update tests.
4. Handle expected errors.
5. Write user-facing messages in clear Russian.
6. Keep functions and modules reasonably small.
7. Do not leave dead code or duplicate implementations.
8. Do not commit credentials, generated databases, browser profiles, or large crawl archives.

After implementation:

1. Run relevant tests.
2. Run the application or relevant verification command where possible.
3. Report exactly which commands were run.
4. Report failures honestly.
5. Update `TASKS.md`.
6. Update `README.md` when setup or usage changes.
7. Update `DECISIONS.md` when an architectural decision changes.
8. Update `CHANGELOG.md` when a user-visible capability changes.

## Definition of done

A task is complete only when:

- the requested user flow works from beginning to end;
- the feature is accessible through the interface where applicable;
- expected error cases are handled;
- relevant tests pass;
- documentation is updated;
- verification steps are provided in Russian;
- no mandatory paid dependency has been introduced.

## Crawler safety

- Respect `robots.txt` where applicable.
- Use a descriptive user agent.
- Use rate limits and delays.
- Do not create excessive concurrent requests.
- Set timeouts.
- Limit crawl depth and page count through configuration.
- Avoid logout links, destructive actions, infinite calendars, faceted-navigation traps, and repeated URL variants.
- Normalize URLs before storing them.
- Never submit forms automatically unless explicitly approved.
- Do not bypass authentication, CAPTCHAs, access controls, or anti-bot protections.

## Data and reliability

- Do not delete historical crawl data without explicit confirmation.
- Use transactions for multi-step database operations.
- Keep backups possible through ordinary file copying or documented export.
- A failed crawl must not corrupt earlier successful crawl results.
- Record crawl status, timestamps, errors, and partial completion.
- Clearly distinguish missing data from zero values.

## Analysis integrity

The application must label conclusions as:

- fact;
- calculated metric;
- correlation;
- hypothesis;
- recommendation.

Do not present a hypothesis as a confirmed cause.

## Communication format after every task

Respond in Russian using this structure:

### Что сделано

A short non-technical description.

### Какие файлы изменены

List each changed file and its purpose.

### Как проверить

Numbered steps that a non-programmer can follow.

### Проверки

List exact test and verification commands and their results.

### Ограничения или проблемы

State remaining limitations or failures.

### Следующий логичный шаг

Suggest one next step only. Do not begin it unless requested.

## Stop conditions

Stop and explain instead of proceeding when:

- required credentials are unavailable;
- an operation may destroy user data;
- the requested approach requires a mandatory paid dependency;
- requirements contradict `PROJECT.md`;
- tests reveal data-loss or security risks.

Do not stop merely because a task is difficult. Implement the safe verified portion and report the limitation.

## Mandatory task order and documentation updates

Work strictly in the approved task order.

Do not start the next task before the current task is implemented, verified, and documented.

After every successfully completed task:

1. update `TASKS.md`;
2. update `PROJECT_LOG.md`;
3. update `ROADMAP.md` when needed;
4. update `CHANGELOG.md` when needed;
5. update `DECISIONS.md` if a new decision was made;
6. update `PROJECT.md` if project rules changed.

If a task is blocked, record the blocker in `TASKS.md`. Do not skip to a later task unless the plan is explicitly revised.
