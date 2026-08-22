"""A Cypher grammar the ontology can't be violated through.

seocho validates generated Cypher *after* the model writes it, and repairs by generating
again. Measured on MARA gpt-oss-120b in the seocho_native arm: 8 generations took 9 attempts,
and one question burned 7 generations (30.9 s of GPU) and still answered wrong. Every repair
is a full prompt re-prefill plus a full decode, so the validator's rules cost real serving
time each time the model fails to guess them.

The rules are structural, though, and the ontology is strict — which makes them expressible as
a grammar instead of a rejection. Constrained decoding cannot emit an undeclared label, cannot
omit the tenant scope, and cannot forget `LIMIT $limit`, so those failures stop existing rather
than being caught. vLLM 0.27 takes an EBNF through
`extra_body={"structured_outputs": {"grammar": ...}}` (the `guided_grammar` of older versions
is gone; `StructuredOutputsParams` in sampling_params.py is the current shape).

The envelope matters: seocho's text2cypher contract is a JSON object with a `cypher` key, so the
grammar produces *that*, with the query inside the string. That is only safe because the policy
already forbids inlined literals — parameters instead — so a conforming query contains no
double quote to escape.

Relationship variables are mandatory (`[t:TRANSFER]`, never `[:TRANSFER]`). "A variable an
aggregate references must be bound in the pattern" is context-sensitive and no CFG can say
it — and gpt-oss-120b hit exactly that hole twice in a row on the same question, writing
`count(t)` over an anonymous relationship, failing EXPLAIN, and repeating the mistake in the
repair turn. Forcing a name on every relationship does not *prove* the RETURN uses it, but
the model reuses the name it was made to write, and the failure class stopped reproducing.
The cost is one or two tokens on queries that never reference the relationship.

EVERY repetition is bounded — `{0,N}` instead of `*` — because under constrained decoding an
unbounded repetition is an attractor. Measured twice on gpt-oss-120b before the rule became
absolute: unbounded whitespace produced a run of spaces until the token cap truncated the
envelope, and after that was bounded, the repair turn fell into the AND-chain instead —
19,390 characters of `AND a.acct_no = $acct_no` repeated until an 8,000-token cap. The
repetition itself is self-reinforcing: once the model has emitted the same clause twice, the
highest-probability continuation is a third. Bounds turn the failure into a forced stop the
envelope survives. The specific caps (3 extra MATCHes, 5 extra predicates, 7 extra return
items, 4 extra hops in a pattern, 24-char identifiers) cover every query the benchmark
corpus has produced.

Whitespace is bounded to at most one space. `" "*` looked harmless and was a decoding trap:
under constrained decoding the space is always a legal next token, and gpt-oss-120b fell into
it — emitting spaces until max_tokens truncated the JSON envelope mid-query, which surfaced as
`explain_failed:CypherSyntaxError` two questions out of eight. Same family as the digit
runaway below: any unbounded repetition in the grammar is an attractor the model can circle.

A comparison may only be against a parameter, never a number. That rule was missing in the
first version and the benchmark found it immediately: every remaining grammar-mode failure was
`inlined_literal:< 3` / `>= 3` / `= 1`, i.e. the model taking the one door the grammar left
open. The policy forbids inlined literals (each one creates a new plan-cache entry per
entity), so the grammar must too — a rule the validator enforces but the grammar permits is
just a slower rejection.

Deliberately a subset. It covers what the measured generations actually used: chained MATCH
patterns with optional bounded variable-length relationships, an optional WHERE comparing
properties against parameters, RETURN with aliases and the aggregate functions the questions
need, optional ORDER BY, and a mandatory LIMIT $limit. A question this subset cannot express is
a finding about the subset, not a licence to widen it silently — `covers()` exists so the
benchmark can report which questions fall outside it.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_AGGREGATES = ("count", "sum", "avg", "min", "max", "collect")


def _alt(values: Iterable[str]) -> str:
    """EBNF alternation of literal strings, longest first so prefixes cannot shadow."""
    uniq = sorted({v for v in values if v}, key=lambda s: (-len(s), s))
    return " | ".join(f'"{v}"' for v in uniq) or '"NONE"'


def grammar_from_policy(policy: Any, *, params: Sequence[str],
                        workspace_property: Optional[str] = None,
                        max_hops: Optional[int] = None) -> str:
    """An EBNF that only admits Cypher this policy would accept."""
    labels = tuple(getattr(policy, "allowed_labels", ()) or ())
    rels = tuple(getattr(policy, "allowed_relationships", ()) or ())
    props = tuple(getattr(policy, "allowed_properties", ()) or ())
    ws_prop = workspace_property or getattr(policy, "workspace_property", "_workspace_id")
    hops = int(max_hops or getattr(policy, "max_graph_hops", 4) or 4)
    # `$limit` and `$workspace_id` are mandatory by contract, so they are terminals rather
    # than choices; the rest of the parameter list is what the harness bound for this question.
    param_names = tuple(p for p in params if p not in ("workspace_id",))

    return f'''root ::= "{{" ws "\\"cypher\\"" ws ":" ws "\\"" query "\\"" ws "}}"

query ::= match_clause (ws match_clause){{0,3}} (ws where_clause)? ws return_clause (ws order_clause)? ws limit_clause

match_clause ::= "MATCH " pattern
pattern ::= node (rel node){{0,4}}
node ::= "(" var? label_part scope ")"
label_part ::= ":" label
scope ::= " {{" wsp "{ws_prop}" wsp ":" wsp "$workspace_id" wsp "}}"
rel ::= arrow_l rel_detail arrow_r
arrow_l ::= "-" | "<-"
arrow_r ::= "->" | "-"
rel_detail ::= "[" var ":" reltype hop_bound? "]"
hop_bound ::= "*1.." digit
digit ::= {_alt(str(i) for i in range(1, hops + 1))}

where_clause ::= "WHERE " predicate (" AND " predicate){{0,5}}
predicate ::= ref ws comparator ws param
comparator ::= "=" | ">=" | "<=" | ">" | "<" | "<>"
param ::= {_alt("$" + p for p in param_names) if param_names else '"$limit"'}

return_clause ::= "RETURN " ret_item (", " ret_item){{0,7}}
ret_item ::= (aggregate | ref | "DISTINCT " ref) (" AS " var)?
aggregate ::= agg_fn "(" ("DISTINCT " )? ref ")"
agg_fn ::= {_alt(_AGGREGATES)}

order_clause ::= "ORDER BY " ref (" DESC" | " ASC")?
limit_clause ::= "LIMIT $limit"

ref ::= var ("." prop)?
label ::= {_alt(labels)}
reltype ::= {_alt(rels)}
prop ::= {_alt(props)}
var ::= [a-z] [a-z0-9_]{{0,24}}
ws ::= " "?
wsp ::= " "?
'''


def covers(cypher: str, policy: Any) -> Tuple[bool, List[str]]:
    """A cheap structural check that a query is inside the subset the grammar admits.

    Not a parser — a way for the benchmark to say "this question needs constructs the grammar
    does not have" instead of quietly attributing the failure to the model.
    """
    reasons: List[str] = []
    text = " ".join(cypher.split())
    upper = text.upper()
    if "LIMIT $LIMIT" not in upper:
        reasons.append("missing LIMIT $limit")
    ws_prop = getattr(policy, "workspace_property", "_workspace_id")
    if ws_prop not in text:
        reasons.append(f"missing scope {ws_prop}")
    if '"' in text or "'" in text:
        reasons.append("string literal (policy requires parameters)")
    for kw in ("WITH ", "UNWIND ", "CALL ", "UNION", "OPTIONAL MATCH", "CASE "):
        if kw in upper:
            reasons.append(f"outside subset: {kw.strip()}")
    return (not reasons), reasons
