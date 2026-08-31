# Invalid for aggregate analysis

The launcher ended before the runner wrote its final report and trace receipt. Two durable
episodes remain for audit: one validator rejection and one completed episode with a failed
verifier-directed repair generation. Do not resume or aggregate this partial output; a fresh
run uses a persistent launcher so the final receipts can be verified.
