# What makes this "agentic"

A chatbot answers. An agent **decides on an action, takes it through tools, checks the result, and stops or asks for help**. Here that means:
1. It plans which tool to call (order lookup, policy search, refund proposal).
2. It acts only through schema-checked, allow-listed tools.
3. It verifies side effects against the record, so a tool that claims "ok" is checked (`test_lying_tool_detected_by_side_effect_check`).
4. It escalates billing disputes and approval-bound actions to people.
5. It is bounded by step caps derived from the conversation budget, and repeated tool calls are flagged.

Requests that need no reasoning (greetings, simple status) skip the model entirely (`test_greeting_uses_no_model`). Using an agent everywhere is a cost bug.
