# Capacity and cost (illustrative example prices, not a quote)

<!-- run: python -m scripts.estimate_10m --scale 1 -->
```text
{
  "inputs": {
    "users": 10000000.0,
    "requests_per_user_day": 5,
    "tokens_in": 2000,
    "tokens_out": 1000,
    "peak_factor": 3.0,
    "mix": {
      "small": 0.75,
      "mid": 0.2,
      "frontier": 0.05
    },
    "turns_per_conversation": 4,
    "target_per_conversation_usd": 0.01
  },
  "estimate": {
    "requests_per_day": 50000000.0,
    "avg_qps": 578.703704,
    "peak_qps": 1736.111111,
    "tokens_per_day": 150000000000.0,
    "tokens_per_month": 4500000000000.0,
    "cost_per_request": 0.003375,
    "cost_per_day": 168750.0,
    "cost_per_month": 5062500.0,
    "cost_per_year": 61593750.0,
    "cost_per_conversation": 0.0135
  },
  "budget": {
    "per_turn": 0.003375,
    "per_conversation": 0.0135,
    "target": 0.01,
    "within_budget": false,
    "per_turn_looks_ok_but_conversation_fails": true
  },
  "step_cap_frontier": 0
}
```
<!-- /run -->

**Per-turn vs per-conversation.** At $0.003375 a turn, one turn looks fine against a $0.01 target. A 4-turn conversation, though, costs $0.0135 and misses the target. The solver finds a mix that fits: 95% small and 5% frontier gives $0.00942 per conversation. At 10x users (`--scale 10`), peak QPS is 17,361 and cost scales linearly to about $50.6M a month at the default mix. Linear cost growth is why the router and the cache are budget items, not optimisations.
