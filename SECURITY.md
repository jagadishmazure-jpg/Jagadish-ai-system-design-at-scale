# Security policy

This is a portfolio project. It runs offline on fictional data (Quillmoor Market). There are no live services, no real credentials and no customer data in the repo.

## Reporting a vulnerability
Use GitHub **private vulnerability reporting** (Security tab → "Report a vulnerability"). Do not open a public issue for security problems, and do not include real personal data.

## Controls
- CodeQL (python, actions), gitleaks over full history, secret scanning with push protection, Dependabot.
- All GitHub Actions are pinned to full commit SHAs.
- API keys are stored as sha256 only. Refresh tokens rotate, and reuse revokes the family.
- PII is redacted from traces, cache and feedback.
- No IaC is ever applied from CI. There are no cloud credentials in the repo or in CI.
