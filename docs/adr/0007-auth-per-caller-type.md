# ADR 0007: One auth mechanism per caller type
**Status:** accepted. **Decision:** OIDC/JWT for customers, Redis sessions for internal tools, hashed API keys for partners. **Consequences:** three code paths, each tested. Depth lives in Jagadish-agent-auth-security.
