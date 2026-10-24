# Capacity estimate

Do the arithmetic before you pick components. `sysdesign/capacity.py` does it,
and `python scripts/capacity.py key=value ...` lets you change any input. The table
below is generated from the defaults and checked in CI.

<!-- BEGIN CAPACITY -->
| Quantity | Value |
| --- | ---: |
| Requests per day | 50,000 |
| Average RPS | 0.579 |
| Peak RPS (x10) | 5.79 |
| Peak concurrent requests (Little's law) | 7 |
| LLM calls per month | 840,000 |
| Tokens per month | 1,512,000,000 |
| LLM spend per month (ladder + cache) | $1,393.20 |
| LLM spend per month (everything on the large model) | $10,125.00 |
| Allowed downtime per month at 99.9% | 43.2 min |
<!-- END CAPACITY -->

## How to read it

- **Average vs peak RPS.** Daily volume divided by 86,400 is the average. Real
  traffic bunches up (evenings, sale days), so the peak multiplier is the
  number to plan replicas for, not the average.
- **Tokens drive cost, not requests.** Monthly tokens are requests × (input +
  output tokens). The default per-request token counts assume retrieval context,
  which dominates the input.
- **Ladder + cache.** The blended price uses the share of traffic each rung
  serves and removes cache hits. Compare it with the all-large figure to see
  why the router exists.
- **Downtime budget.** A 99.9% target allows about 43 minutes a month. One bad
  deploy can use all of it, which is why fallback and stateless replicas matter.

Prices are illustrative per-million-token tiers (`sysdesign/providers.py`,
as-of date in that file), not quotes from any vendor.
