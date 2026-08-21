# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

---

# Part 1 — What this repo measures, and what makes a run valid

Read this part before running anything. The commands are reproducible from
`README.md` and `docs/testbed.md`; what is not reproducible from them is *why a number
counts*, and a run that violates one of the invariants below produces a plausible number
that has to be thrown away.

## The subject

This repo measures **the exchange between an agent and a graph database as the graph grows**
(DozerDB/FinBench, SF1→SF100), and — since the testbed — **the exchange between the agent and
the model server** as well. Nine arms in `scripts/agents/agent_interaction.py:277` vary one
interface decision at a time: what schema the model sees, whether a guardrail refuses its
query, whether it may steer the planner, whether rows enter its context at all.

The deliverable is **before/after of specific engineering interventions**. It is not "the
ontology helps": a chart that only says "ontology good" is not evidence about anything an
engineer can do differently on Monday. Every arm exists to isolate one intervention, and
every claim is paid for in two currencies at once — what the model spends (tokens, round
trips, context) and what the database spends (db hits, latency, lock queueing).

## Invariants — break one and the number is void

1. **Manifest and raw samples, always.** Use `scripts/analysis/runmeta.py`
   (`manifest()`), write per-iteration samples, never aggregates alone. A median without
   samples cannot be re-analysed, and a latency figure that cannot name the commit, decoder,
   container image, GPU and serving flags that produced it is not reproducible.
2. **One repo, one source.** Figures for this talk are built from *this* repo's `results/`.
   If a requested figure needs data that does not exist here, say so and propose measuring
   it here — never substitute a lookalike dataset from a sibling repo. And never change the
   underlying data and the visual form in the same step without flagging the data change.
3. **A vLLM run is a new arm, never a replacement.** The published arms ran against a hosted
   endpoint with no prefix cache. A self-hosted vLLM is a different serving stack; putting
   its numbers into an existing figure makes results appear to *change* between iterations,
   which destroys trust in every chart in the deck.
4. **The endpoint is a measured variable.** `harness/llm.py` resolves provider, model and
   base_url once and the run records the descriptor (`endpoint` in the episodes JSON,
   `endpoint.json` in a runner run). If that block does not say what you assumed, the run is
   void — not "close enough".
5. **Controls are explicit or they are not controls.** vLLM V1 enables prefix caching by
   default, so the control arm *must* pass `--no-enable-prefix-caching`
   (`PREFIX_CACHING=off`). The probe's salted-prefix arm exists for the same reason: it
   separates prefix reuse from a server that merely warmed up. An unflagged control measures
   the treatment twice and reads as "no effect".
6. **The harness owns the workspace scope, the row cap and the anchor — never the model.**
   Receipt: an anchor the model inlined as a literal is the defect that turned one question
   into 38 million db hits (`scripts/agents/agent_interaction.py:449`). A harness that owns
   the scope is also what a real subject-scoped service does.
7. **Never compare across denominators.** Three live traps: vLLM's Prometheus prefix-cache
   counters are *token*-denominated while its 5-second log line reports a *block*-denominated
   rate over the last 1k queries; server-side TTFT starts at the frontend's `arrival_time`
   while the probe's starts at the client; monotonic timestamps from different processes are
   not comparable at all.
8. **A hardcoded constant in a chart is a claim.**
   `python scripts/analysis/check_chart_provenance.py` must exit 0 before any figure ships.
9. **Know which layer is deterministic.** The generator is seeded — regenerating SF1
   reproduces the committed manifest's checksums exactly, so `GENERATE_MISSING=1` on a fresh
   clone gives the same graph, not a similar one. Episodes involve a model and will not
   reproduce exactly; that is precisely why raw samples are kept rather than only medians.

## Reproducing on a rented GPU

Procedure: `docs/testbed.md`. Before trusting a run, check these, in order — each one has
failed at least once, and all but the last fail *silently*:

| Gate | What good looks like |
| --- | --- |
| bulk load | `imported` node/relationship counts equal the snapshot manifest's `counts`; SF1 = 4,083 / 27,454 |
| index | `EXPLAIN MATCH (a:Account {acct_no:$a,_workspace_id:$ws})` plans a `NodeIndexSeek`, not a label scan |
| anchor | the `[gold]` line prints a real anchor and `p99_out`, not `anchor=None` |
| endpoint | the `[endpoint]` line names the provider, model and base_url you intended |
| resume | re-running with `--resume` reports the episodes already in the log and runs 0 new ones |
| cache A/B | the stable arm's warm `cached_tokens` is near `prompt_tokens` **and** the salted arm's is 0 |
| durability | `results/runs/<id>/metrics.jsonl` is non-empty, and the destroy gate passed before the instance was destroyed |

## Traps with receipts

Each of these cost real time once. They are documented so they cost it only once.

| Trap | How it presents | Where |
| --- | --- | --- |
| `Account._out_degree` missing after a load | every question runs against anchor `None` — an empty run, not an error | computed in `scripts/data/export_import_csv.py` |
| prefix caching on by default (vLLM V1) | the "control" arm shows the same speedup as the treatment | `testbed/serve_vllm.sh`, `PREFIX_CACHING=off` |
| `--enable-prompt-tokens-details` off by default | `prompt_tokens_details: null`; server counters move but cannot be attributed to an episode | `vllm/entrypoints/openai/cli_args.py:132` |
| KV residency metrics off, then sampled at 1% | `kv_block_*` histograms empty at our request volume | `--kv-cache-metrics-sample`, raised in the serve script |
| gpt-oss tool calls | do **not** pass `--tool-call-parser`; `HarmonyParser` handles it | `vllm/tool_parsers/gptoss_tool_parser.py:19` |
| gpt-oss empty final turn | closing turn spent in the reasoning channel, empty content; harness answers with one recorded nudge (`nudged`) | seen on the hosted endpoint; may follow the model |
| `outputs/` is gitignored | a fresh clone has no snapshots and the loader has nothing to load | `GENERATE_MISSING=1`, or seed from S3 |
| a vast.ai instance is itself a container | anything needing `docker exec` or a compose stack fails there | `bulk_load.py --exec-mode local`; `scripts/testbed/metrics_sampler.py` instead of the dashboards |
| one client per episode | hundreds of episodes in, "too many open files" rather than a clean failure | `chat_model()` memoises per endpoint |
| pushing a workflow file | rejected: the push credential has no `workflow` scope | ships at `testbed/ci/`, copied into `.github/workflows/` by hand |

## What not to do

- Do not tune the model, the prompt or the sampling to make an arm look better. The arms
  differ by one interface decision; anything else that changes is a confound.
- Do not add a metric to a figure because it is available. Every number in the deck has to
  be traceable to a file in `results/`.
- Do not delete or rewrite a published result to make a new run agree with it. Add the new
  run as its own arm and let the two disagree in the open.

---

# Part 2 — Tooling conventions

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Patterns that match your own shell:**
```bash
pkill -f "vllm[ ]serve"     # NOT: pkill -f "vllm serve"
```
`pkill -f` matches full command lines — including the shell running the `pkill`, whose own
command line contains the pattern. The unbracketed form kills the shell that issued it, which
reads as a mysterious non-zero exit rather than as a self-inflicted kill.

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION -->
## Issue Tracking with bd (beads)

**IMPORTANT**: This project uses **bd (beads)** for ALL issue tracking. Do NOT use markdown TODOs, task lists, or other tracking methods.

### Why bd?

- Dependency-aware: Track blockers and relationships between issues
- Version-controlled: Built on Dolt with cell-level merge
- Agent-optimized: JSON output, ready work detection, discovered-from links
- Prevents duplicate tracking systems and confusion

### Quick Start

**Check for ready work:**

```bash
bd ready --json
```

**Create new issues:**

```bash
bd create "Issue title" --description="Detailed context" -t bug|feature|task -p 0-4 --json
bd create "Issue title" --description="What this issue is about" -p 1 --deps discovered-from:bd-123 --json
```

**Claim and update:**

```bash
bd update <id> --claim --json
bd update bd-42 --priority 1 --json
```

**Complete work:**

```bash
bd close bd-42 --reason "Completed" --json
```

### Issue Types

- `bug` - Something broken
- `feature` - New functionality
- `task` - Work item (tests, docs, refactoring)
- `epic` - Large feature with subtasks
- `chore` - Maintenance (dependencies, tooling)

### Priorities

- `0` - Critical (security, data loss, broken builds)
- `1` - High (major features, important bugs)
- `2` - Medium (default, nice-to-have)
- `3` - Low (polish, optimization)
- `4` - Backlog (future ideas)

### Workflow for AI Agents

1. **Check ready work**: `bd ready` shows unblocked issues
2. **Claim your task atomically**: `bd update <id> --claim`
3. **Work on it**: Implement, test, document
4. **Discover new work?** Create linked issue:
   - `bd create "Found bug" --description="Details about what was found" -p 1 --deps discovered-from:<parent-id>`
5. **Complete**: `bd close <id> --reason "Done"`

### Auto-Sync

bd automatically syncs with git:

- Exports to `.beads/issues.jsonl` after changes (5s debounce)
- Imports from JSONL when newer (e.g., after `git pull`)
- No manual export/import needed!

### Important Rules

- ✅ Use bd for ALL task tracking
- ✅ Always use `--json` flag for programmatic use
- ✅ Link discovered work with `discovered-from` dependencies
- ✅ Check `bd ready` before asking "what should I work on?"
- ❌ Do NOT create markdown TODO lists
- ❌ Do NOT use external issue trackers
- ❌ Do NOT duplicate tracking systems

For more details, see README.md and docs/QUICKSTART.md.

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

<!-- END BEADS INTEGRATION -->
