This gate is not evidence. The `physical_only` arm made paid MARA calls and was correctly
rejected by the guardrail, but the exception record copied usage only on successful query
execution and therefore reported zero tokens. Raw artifacts are retained unchanged. The
replacement runner creates an attempt record before each request and retains usage and trace
IDs for validation rejection, timeout, and endpoint error paths.
