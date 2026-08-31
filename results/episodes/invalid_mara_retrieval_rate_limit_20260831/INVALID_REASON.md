# Invalid: MARA rate limit and interrupted matrix

This run is retained for audit only and must not enter an aggregate or figure.

- Intended design: DeepSeek-V3.1, SF1 and SF100, four questions, `multi_full` and
  `multi_typed`, two repeats (32 episodes).
- Preserved before termination: 26 append-only episode samples and 43 system samples.
- Fifteen episodes ended with MARA `RateLimitError` before graph execution; eleven executed.
- The run was stopped to avoid paying for repeated transient failures. It has no completed
  report, system-monitor receipt or trace receipt.
- Source manifest remains useful for reconstructing the attempted protocol, but this is not
  a partial performance result. A replacement must use a new run ID, explicit request pacing
  and a preflight/cooldown policy.
