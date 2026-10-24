# ADR 0005: RollingUpdate with maxUnavailable 0 and readiness gating
**Status:** accepted. **Decision:** surge one pod at a time. A pod gets traffic only after `/readyz` passes. **Consequences:** deploys need a little spare capacity. A broken release stalls instead of serving (proved on kind in CI). Blue/green and canary are left for later.
