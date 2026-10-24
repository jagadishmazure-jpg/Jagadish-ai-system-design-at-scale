# What makes this "agentic"

A chatbot answers. An agent picks an action, takes it through tools, checks what happened, and stops or hands off. In Kestrel Goods' support assistant that means:
1. It decides between a rule answer, an order lookup, a policy search or a refund job (main's intent ladder in `sysdesign/router.py` and `sysdesign/agent.py`).
2. It acts only through typed, allow-listed tools; an order lookup is scoped to the caller (`test_other_users_order_not_revealed`).
3. Prices and amounts come from tools, never from model text (`test_price_comes_from_tool_not_model`), and numbers in an answer must be grounded (`test_grounding_blocks_invented_number`).
4. Disputes go to a person with a redacted note (`test_escalation_goes_to_human_with_redacted_note`).
5. Steps are capped by the conversation budget (`test_step_cap_from_budget`).

Requests that need no reasoning skip the model (`test_rules_answer_without_llm`). Running an agent loop for everything is a cost bug.
