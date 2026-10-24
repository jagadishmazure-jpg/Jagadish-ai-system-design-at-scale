# ADR 0006: Desired state in git; rollback is git revert
**Status:** accepted (Argo CD planned). **Decision:** `deploy/version.yaml` is the only deploy lever, and the alert handler reverts its last commit. **Consequences:** every deploy and rollback is reviewable history. A sync controller is needed to close the loop automatically.
