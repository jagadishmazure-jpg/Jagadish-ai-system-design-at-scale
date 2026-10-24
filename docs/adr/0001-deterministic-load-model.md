# ADR 0001: a deterministic load model instead of a live benchmark

**Status:** accepted

**Context:** The README has to show before and after numbers that a reviewer
can reproduce. CI must verify them on free runners, with no cloud and no
provider keys.

**Decision:** Drive the real components with a discrete-event model in virtual
time, using seeded randomness and fake providers. Commit the JSON and have CI
fail on drift.

**Consequences:** The numbers are reproducible and cheap to produce, but they
are model outputs, and the docs label them that way everywhere. Real-hardware
load testing (for example k6 against compose) is listed as planned.
