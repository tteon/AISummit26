# Invalid for performance or repair-loop analysis

The MARA endpoint accepted the planner call but rate-limited the first executor call (HTTP
429). The harness stopped after writing the durable partial sample. No graph execution or
repair loop completed, so this directory must not enter an aggregate or paired comparison.
The endpoint descriptor, partial API usage, local/Tempo trace receipt and database monitoring
receipt are retained for audit only.
