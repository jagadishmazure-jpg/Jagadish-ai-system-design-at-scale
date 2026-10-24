# ADR 0005: In-process simulated load, labelled as such
**Status:** accepted. **Context:** no paid infrastructure. **Decision:** the harness runs in-process against fake providers with configured latency and capacity. Results are labelled "simulated" and quoted with sample sizes. **Consequences:** they show the direction of an effect, not capacity. Real-cluster numbers come only from kind in CI.
