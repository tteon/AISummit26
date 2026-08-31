# Real-world request to FinBench execution contract

The Agent API requires three stages:

```text
business words / user request
  -> semantic term (FIBO proxy, informative term, or local extension)
  -> physical FinBench labels, relationships, properties and derived expressions
```

[`ontology/business_request_finbench.mapping.yaml`](../ontology/business_request_finbench.mapping.yaml)
is the business-vocabulary layer. A recognized term is not necessarily executable: unsupported
terms return a typed refusal, never invented Cypher.

| Business phrase | Semantic disposition | Executable mapping / response |
| --- | --- | --- |
| transaction, transfer | FIBO `individual_transaction`, informative only | `TRANSFER` relationship; never `:Transaction` or `:Transfer` |
| beneficial owner | FIBO proxy | `(:Person|Company)-[:OWN]->(:Account)`; labels, not `owner_type` |
| total repaid | derived measure | `sum((:Account)-[:REPAY]->(:Loan).amount)`; never `Loan.total_repaid` |
| payment rail | informative FIBO payment-service intent | `Channel` plus `USES_CHANNEL` and `TRANSFER.channel_risk` |
| corporate control | proxy with limitation | `INVEST` is holding evidence, not proof of legal control |
| subsidiary / owner type | unsupported | typed `UNSUPPORTED_RELATIONSHIP` / `UNSUPPORTED_SEMANTIC_FIELD` refusal |

This resolves the six matched MARA schema rejections before Cypher generation. The paired
repair arm used more model work and had one fewer correct answer, so resolving semantics in an
unbounded repair loop is the wrong control point.

## Expanded request catalog

[`configs/agentic_request_schema_contracts_v2.yaml`](../configs/agentic_request_schema_contracts_v2.yaml)
is a separate versioned future arm. It preserves the paid v1 denominator and adds 12 harder
requests spanning customer service, AML, fraud/device investigation, KYC/UBO, underwriting
and collections. Each item declares exact physical direction, typed inputs, expected result
and verification invariants, a bounded plan-risk class, and a gold Cypher statement.

It includes shared-device/common-owner intersections, applicant-to-facility joins,
repayment-ratio aggregation, a cash-exit cycle, and a policy-capped three-hop layering path.

## Zero-cost pre-flight

```bash
python3 scripts/analysis/validate_business_request_mapping.py
python3 scripts/analysis/validate_request_schema_contracts.py
```

The gates reject unknown names, nonexistent properties, impossible endpoints, unregistered
semantic references, incomplete result contracts, and unsupported terms without typed refusal.
The eventual comparison must vary only physical-schema versus contract-backed context, holding
endpoint, model, workload, bound parameters, row cap and scale fixed and saving raw episodes,
endpoint descriptors and PROFILE/Bolt traces.

## Two interface problems beyond ontology mapping

### 1. Repair-loop admission and authority

Schema mapping answers what can be compiled; it does not decide whether another model call is
worth making. The paired MARA authority sample is the receipt: allowing automatic verifier
repair added 8 model calls, 9,795 prompt tokens, 3 graph trips and 289 DB hits, but finished
with one fewer correct answer than advisory verification. The six identical schema rejections
were pre-execution semantic failures, so a generative repair was not a useful remedy.

The API should make a typed decision before a repair stage:

| Failure class | Action | Additional model/graph work |
| --- | --- | --- |
| `unknown_label`, `unknown_property`, `unsupported_semantic` | resolve through this mapping or return a refusal | zero executor repair calls |
| unbound user/policy value, excess hop/row budget | bind or refuse through harness policy | zero graph calls |
| executable query whose typed result violates the ResultEnvelope | bounded repair may be admitted | charged to request repair budget |
| database/transient driver failure | retry transport with an explicit retry class | not semantic repair |

`RepairBudget` should cap additional model calls, prompt tokens, graph trips, DB hits and path
hops per user request. The trace must record the decision code and the rejected alternative,
so an aggregate distinguishes a refused semantic mismatch from a costly repair.

### 2. Context isolation and typed handoff

Multi-agent failure is often a boundary failure, not an individual-agent failure. Passing the
whole conversation lets a planner's speculative business phrase become an executor's physical
identifier, and lets verifier prose override harness-owned anchor/scope/limit. It also makes
token cost grow with conversation history even when only the final physical query matters.

Use immutable typed envelopes instead of transcript handoff:

```text
planner:  UserRequest + business mapping -> RequestContract
executor: RequestContract + harness bindings -> ExecutionReceipt (Cypher, params, PROFILE/Bolt)
verifier: RequestContract + ExecutionReceipt -> VerificationDecision
```

The planner receives aliases, FIBO/local semantic status and unsupported terms. The executor
receives only canonical physical surface and typed bindings. The verifier receives only the
request/result contract, execution receipt and cost ledger; it does not receive the full
conversation by default. This preserves context isolation while making every cross-agent fact
inspectable.

Measure this independently of ontology mapping: keep endpoint, model, requests, anchors,
row cap and scale fixed, then compare full-history, short-window, case-file and typed-envelope
handoff. Per raw episode, record context/prompt tokens, semantic refusals, admitted repairs,
correctness, graph trips, PROFILE hits, DB milliseconds and wall time. Do not combine that arm
with the schema-mapping arm; each changes a separate interface decision.
