---
id: 5
tags: [reading]
created_at: '2026-06-20T20:00:00Z'
updated_at: '2026-07-10T21:30:00Z'
---

# The Pragmatic Programmer

Hunt, Thomas — 20th anniversary edition (2019). Software engineering principles.

## Status

Finished 2026-07-10. Would re-read the middle chapters on estimation and refactoring.

## Core ideas

**DRY — Don't Repeat Yourself.** Every piece of knowledge must have a single, authoritative representation. Not just code — documentation, build processes, data schemas.

**Orthogonality.** Changes to one component should not ripple into unrelated ones. A helicopter pilot training example: each control is independent; good systems behave the same way.

**Tracer bullets vs. prototypes.** Tracer code is lean but production-ready (error handling, logging) and deployed; prototypes are throw-away experiments. Don't confuse the two.

**Broken windows.** One bad line of code left unchallenged signals that standards are optional — entropy spreads. Fix small things immediately.

**The power of plain text.** Data in human-readable form outlives every proprietary format. (relay is built on exactly this principle.)

## Highlights

> "Don't live with broken windows."

> "It's easier to ask forgiveness than permission." — On experimentation: try, learn, course-correct.

> "The greatest of all weaknesses is the fear of appearing weak."

## Chapters to revisit

- Ch. 6 — Concurrency (STM, actors) — concepts aging but principles still sharp
- Ch. 8 — Pragmatic Projects — estimation techniques
- Ch. 9 — Before the Project — requirements as dynamic process

## Related

- *A Philosophy of Software Design* (Ousterhout) — complementary, heavier on modularity
- *Clean Code* (Martin) — more prescriptive, polarising; read with scepticism
