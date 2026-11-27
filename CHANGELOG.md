# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this is a runtime skeleton rather than a library, `0.x` versions may
change contracts. Any change to `contracts.py` will be called out explicitly
under **Breaking**, with a migration note.

---

## [0.7.1] - 2026-11-27

Documentation restructure. No code changes.

`CAPACITY.md` had grown by appending: every re-measurement added a section, so
the file ended up with two soak chapters, two multi-worker chapters, and a pile of
retracted numbers sitting beside the current ones. The data was right; the
document made a reader do the sorting.

Rewritten around a fixed structure:

| Section | Content |
|---|---|
| §0 | Lookup table — every current number with its conditions and where it came from |
| §1-§8 | One dimension each, with the earliest measurement demoted to a sub-section |
| §9 | What is still open |
| Appendix A | **How to measure** — six disciplines that each came from a mistake |
| Appendix B | **Retracted conclusions** — kept as evidence, marked do-not-cite |

Appendix A is the part worth reading. It records why the load generator's own
ceiling has to be checked first, why warmup has to be dropped, why phase means
beat both linear fits and trough slopes on sawtooth data, how to tell server
saturation from network saturation by watching CPU rather than RPS, that
long-running local processes need `start_new_session=True`, and that cleanup
should go by PID because `pkill -f` matches the shell running it.

Appendix B keeps the 15x throughput error, the false memory-leak alarm, the
"multi-worker does nothing" conclusion, and the "cache billing is untestable"
framing. Each has the wrong conclusion, why it was wrong, and what the correction
taught. Deleting them would have removed the only evidence of how these
measurements go wrong.

Also synced the 20-minute figures in `SLO.md` and `RUNBOOK.md` to the 150-minute
run (p95 91.7ms, p99 101.8ms, phase means 101.3 / 101.3 / 97.1MB).

All 10 internal anchor links verified against GitHub's slug rules.

---

## [0.7.0] - 2026-11-27

Closes the last two "unverified" items. One produced a number; the other produced
a negative result with evidence behind it.

### Measured: the full worker scaling curve

A 64-core x86 machine made it possible to test past 8 workers, which the 10-core
dev box could not.

| workers | RPS | multiple of 1 |
|---|---|---|
| 1 | 329 | 1.0x |
| 2 | 640 | 1.9x |
| 4 | 1,347 | 4.1x |
| 8 | 2,967 | 9.0x |
| 16 | 6,754 | 20.5x |
| 32 | 16,280 | 49.4x |
| 48 | 21,106 | 64.1x |
| 56 | **21,482** | **65.3x** |
| 64 | 21,351 | 64.9x |

Zero errors throughout. The plateau sits between 48 and 56 workers at roughly
**21,500 RPS**; at 64 the box only fit 54 workers (system and load generator need
cores too) so throughput dipped slightly.

Two findings worth keeping:

- **Scaling is superlinear at low counts** (16 workers = 20.5x). A single worker
  cannot be saturated by the load generator because every request queues on one
  GIL. More processes is what lets requests actually run in parallel.
- **Per-core RPS is not portable.** One worker does 329 RPS here and 2,247 RPS on
  the Apple M5 dev box — a 6.8x gap from CPU, Python version (3.12.3 vs 3.14.3)
  and container filesystem, not from code. The portable figure is the scaling
  ratio, not the absolute number.

This also corrects an earlier impression: the "8 workers = 6,911-8,068 RPS"
figure was the dev box's 10-core ceiling, not the server's. Given cores, the same
code reaches **21,500 RPS**, 2.7x that number.

### Measured: cache billing on vLLM — a negative result with evidence

The open question was whether `Usage.billable()` produces the right discount
against a real upstream. Against vLLM 0.32 it cannot, and now we know exactly why.

With `--enable-prefix-caching` and three identical long prompts:

| Observation | Value |
|---|---|
| `/metrics` `prefix_cache_hits_total` | 0 -> **1,248** |
| `/metrics` `prompt_tokens_cached_total` | **1,856** |
| `usage.prompt_tokens_details` | **null** |
| `usage.cached_tokens` | absent |

**The cache works. The usage API does not report it.** No amount of parsing fixes
that, so this is an upstream limitation rather than a bug here.

### Added

`adapters.openai_compat.scrape_prefix_cache_tokens(metrics_url, model_name=...)`
reads the cumulative counter so callers can take a delta over a window. Verified
against the live instance:

```
before 2464 -> after 3072, delta 608
billable at full price 624 -> 77 with the delta applied   (87.7% saved)
```

It is deliberately **not** automatic per request. Scraping around each call only
works serially; under concurrency the delta cannot be attributed to one request
without serialising the window, which would collapse throughput to save
accounting precision. Cumulative value plus caller-side deltas keeps both.

Scraping failures return 0 rather than raising: an observability path must not
become a business dependency.

### Measured: 150-minute soak

2.5 hours, 1,036,418 requests, 99.9998% success, p99 101.8ms, threads constant at
10, file handles flat.

Memory is the interesting part. Phase means came out 101.3 / 101.3 / 97.1 MB —
**flat, even slightly down** — while peaks grew 268 -> 487 MB. Flat means with
growing peaks points at longer GC intervals as the object graph grows, not at a
leak: a leak shows up as each collection leaving more behind, which lifts the
mean.

The criterion changed because of this data. Three methods on the same run:

| Method | Result | Why it misled |
|---|---|---|
| Whole-series linear fit | -0.109 MB/min (negative) | Peak-dominated; the answer depends on whether the last sample landed on a peak |
| Trough slope | +0.68 MB/sample (looks like a leak) | One early dip to 55.2MB set the baseline; troughs are not monotonic |
| **Phase means** | 101.3 -> 97.1 (ratio 0.96) | Adopted. Insensitive to any single outlier |

Two requests out of 1,036,418 returned 502. Server-side `UNRECOVERABLE` count was
**0** (verified), so this is not a Loop-level unrecoverable error; most likely a
transient client-side connection issue at 0.0002%.

Getting to 150 minutes took three attempts. The first two died at ~50 minutes
with a clean SIGTERM shutdown — `server.drained` then `server.stopped`, no crash.
Cause: `nohup ... &` plus `disown` does not detach in a non-interactive shell, so
the process stayed in the session and was cleaned up with it. Fix:
`subprocess.Popen(..., start_new_session=True)`. Worth remembering for any
long-running local process, not just this test.

### Tests

2282, up from 2272. The 10 new ones cover the metric parser (model filtering,
multi-engine summing, comment lines, garbage lines, fail-open on unreachable) and
that the discount actually applies once the delta is supplied.

---

## [0.6.0] - 2026-11-27

Three gaps that were not defects but were not capabilities either. Each is the
answer to a question the HTTP surface could not previously answer.

### Added

- **`GET /v1/tools`** — what this instance actually registered.

  Until now there was no HTTP way to find out. Operators read the YAML, but the
  file and the running state are not the same thing: loading validates, rejects,
  and warns, so a tool in the file may not be registered. This reports the
  post-registration fact.

  `needs_approval` is computed from the risk tier (T2+), so a client preparing
  approval UI does not have to reimplement the tier rules.

  **`secrets` is never returned.** That field holds environment variable names to
  inject at call time, which is deployment-internal. Serialisation is a
  whitelist, not "everything except secrets" — a blacklist would eventually miss
  a field added later.

- **`GET /v1/capabilities`** — what the dispatcher claims it provides
  (`idempotency`, `approval`, `rate_limit`, …). These claims are backed by
  `tests/test_dispatch_capabilities.py`, so they are verifiable rather than
  self-asserted.

- **`POST /v1/admin/tools/reload`** — re-read the tool config and swap the table
  in place.

  Previously, changing one tool parameter required a restart, which interrupts
  in-flight turns. Not a defect, but a real cost for anyone tuning a whitelist.

  Three things about the implementation matter:

  | Property | Why |
  |---|---|
  | **In-place replacement** | `_loop_factory` closes over the dispatcher, which holds a registry reference. Building a new registry and swapping `app.state` only affects loops created afterwards — running loops keep the old table, which is a hard failure to notice |
  | **Fail-closed** | Any error leaves the old table untouched. Swapping in a broken table turns "edit config" into "stop service" |
  | **Independent gate** | `AGENT_RUNTIME_ADMIN_TOKEN`, not the main auth. Changing runtime capability is an administrative action and should not ride on an ordinary user's `runtime:run` scope. Without the token the endpoint is not registered at all, so its existence is not disclosed |

  The reload re-reads the path recorded at startup. Changing the *path* still
  needs a restart, because that is a configuration change rather than a content
  change.

### Fixed

- **`/v1/whoami` returned a bare 404 when auth was disabled.** The status code
  was right — the capability does not exist — but `{"detail":"Not Found"}` sends
  the client looking for a typo in the URL when what it needs is an environment
  variable. It now returns a 404 with `whoami_requires_auth`, an explanation, and
  the switch to flip.

### Not changed

Things I looked at and left alone, with reasons:

- **`toolconfig.py` staying startup-only** is correct for everything except the
  reload case now covered above.
- **Resource planes having no HTTP endpoints** is right: `plane/` is
  implementation, `serve.py` is exposure. The two read-only queries that
  genuinely belong on the surface are the two added here.
- **`/v1/whoami` returning 404 rather than 500 or a default identity** is
  deliberate and stays.

### Tests

2272 total, up from 2258. The 14 new ones cover the whitelist (no `secrets`
leak), the approval flag derivation, token rejection, add/remove diff accuracy,
and the fail-closed case where a broken config must leave the old table working.

No existing endpoint was removed or changed in shape; the four new routes are
additive.

---

## [0.5.0] - 2026-11-27

### Fixed

- **Approval was auto-granted on the HTTP surface.** `Gateway` hardcoded
  `decision=True` when resuming a suspended turn, so any client holding a
  `ctx_id` approved the pending T2+ action simply by sending it again. The
  reject path did not exist at all, which is the one thing an approval gate is
  for. `decision` is now required and explicit; omitting it returns
  `resume_needs_decision` (409) rather than proceeding.

  This is the kind of defect that looks like a missing feature. The state
  machine was complete — `Phase.WAITING_USER`, `Approval`, `loop.resume()` —
  and the demo exercised it. What was missing was the ability to say no.

### Added

- **`POST /v1/turns/{ctx_id}/resume`** — the approval entry point. `decision`
  is a required boolean; `reason` is forwarded to the loop so a rejected model
  can choose a different plan instead of retrying the same action.

  | Status | Meaning |
  |---|---|
  | 200 | Approved and completed, or rejected and the model re-planned |
  | 404 | ctx_id unknown, expired, or not the caller's (deliberately not distinguished) |
  | 409 | ctx_id exists but is not awaiting confirmation |
  | 422 | `decision` missing or malformed |

- `decision` is also accepted on `POST /v1/turns` for clients that would rather
  not switch endpoints. It only means something when `ctx_id` points at a
  suspended turn; on an ordinary turn it is ignored.

- `resume_needs_decision` maps to **409 Conflict**, not 500. Supplying the field
  fixes the request, so it is a state conflict rather than a server fault.

- RUNBOOK section 15 on handling suspended turns, including what not to do:
  do not resend the same goal (that opens a new task and leaves the old one
  hanging), do not omit `decision` and hope, and do not delete the checkpoint
  before deciding, because the `pending_confirmation` and its `args_hash` are
  what make a safe replay possible.

### Verified end to end

A full flow over the HTTP surface, with a prompt-aware fake model:

```
POST /v1/turns                 -> 200  phase=WAITING_USER  tool not executed
POST /v1/turns (no decision)   -> 409  resume_needs_decision
POST /v1/turns/{id}/resume false -> 200  rejected, no side effect
POST /v1/turns/{id}/resume true  -> 200  phase=DONE, executed exactly once
```

The "exactly once" was worth checking separately: a naive mock that answers by
call count rather than by prompt content re-triggers the tool after replay and
makes the run look non-idempotent. The tool result is rendered into the prompt
as `<<<TOOL_RESULTS>>>`, so a model that reads its context converges; one that
counts calls does not.

---

## [0.4.1] - 2026-11-26

Documentation and gates only; no code changes outside the fact checker.

### Fixed

- **README claimed sqlite write contention was unquantified** when
  `scripts/bench_sqlite.py` had already measured it: multi-process writes to a
  single file reach 3.07x with zero failures. Moved to the verified table with
  the actual conclusion — the case for Redis is fault domain, not throughput.
- **README and the site both said six verification gates.** There are seven;
  the count has been wrong since gate 3c (config defaults) was added. Two
  documents repeating the same stale number is why nobody caught it.
- **The site contradicted itself**, saying both six and seven gates in
  different places.

### Added

- `check_readme_facts.py` now derives the gate count from `verify.sh` itself and
  compares it against both documents, accepting Arabic and Chinese numerals
  because the site uses the latter. Verified by deliberately breaking the site
  text and watching the gate fail. The facts check is now eight assertions.
- **RUNBOOK sections 13 and 14**, both from failures encountered this week:
  reasoning chains leaking into turn text, and an upstream that answers 200 and
  appears to reason but never emits a tool call. Each states what not to do,
  since in both cases the tempting fix makes it worse — deleting reasoning from
  history matters more than displaying it, and "the model cannot call tools" is
  the wrong conclusion when the parser is simply mismatched.
- **DEPLOY.md section on local inference backends**, covering the three checks
  that all fail silently: enabling tool choice and picking the right parser for
  the model's template, reasoning stripping being on by default, and
  `--gpu-memory-utilization` budgeting weights plus KV cache rather than weights
  alone (0.99 fails, 0.95 loads but leaves no cache, 0.88 works on a 48GB card
  holding a 25.22 GiB model).

---

## [0.4.0] - 2026-11-26

### Added

- **Reasoning chain stripping** in `OpenAICompatAdapter`, on by default.
  Reasoning models return thoughts and answer in the same `content` string, with
  `message["reasoning"]` left null unless the server is configured with a
  reasoning parser. Measured on vLLM with Qwen3.8-27B:

  ```
  content: 用户问"2+2等于几？只回答数字。" 需要只回答数字。答案是4。\n</think>\n\n4
  ```

  Left alone, that monologue becomes the turn text: the user reads the model
  thinking out loud, and the next turn sees its own reasoning echoed in context.
  With `strip_reasoning=True` the text is `4`.

  The rule is deliberately conservative: if nothing follows the marker, the
  original text is returned. When reasoning consumes the whole `max_tokens`
  budget, content may hold only the pre-marker part, and stripping would turn
  "did not finish" into "answered nothing", which is worse.

### Measured

- **Third upstream: vLLM with a 27B local model.** Qwen3.8-27B 4-bit on a single
  4090. Steady state 4.5-4.8s per call, and a 2-step turn in 16.3s.
- Getting it to serve `tool_calls` took two non-obvious flags. Without
  `--enable-auto-tool-choice` the `tools` parameter returns a 400. With
  `--tool-call-parser hermes` the endpoint returns 200, the model apparently
  thinks, and `finish_reason` is `stop` with `tool_calls: null` — a silent
  failure. This model needs `qwen3_xml`, matching its `<tool_call><function=`
  template.

### Not settled

- Cache-aware billing still has no real upstream confirmation: Strata, vLLM with
  embedding models, and this chat instance all report zero or omit
  `prompt_cache_hit_tokens`.
- Embedding and rerank services were stopped on the 4090 to make room for the 27B
  model, and restored afterwards. Their startup commands are recorded in
  CAPACITY.md.

---

## [0.3.0] - 2026-11-26

Two things this release fixes were found by people other than me, which is the
point of publishing.

### Fixed

- **`pip install agent-runtime` installed someone else's package.** The name on
  PyPI belongs to an unrelated AutoGen placeholder (v0.0.1) that pulls in four
  `autogen-*` distributions and cannot be imported. The README told readers to
  run exactly that command as step one. The distribution name is now
  `turing-agent-runtime`, and the README says plainly that it is not on PyPI yet
  and gives the git-based install.
- **`risk_tier=0` was accepted silently and then removed the tool.** Python does
  not enforce the enum, so a bare int got through construction, and
  `builder._render_tools` threw `AttributeError` on `.name`. `Loop._build_prompt`
  caught that and turned it into a warning, so the turn completed normally with
  every tool missing from the prompt. `ToolManifest.__post_init__` now coerces
  both `risk_tier` and `side_effect`, and rejects invalid values at the line
  where the tool is defined.

### Corrected

- **Every throughput number in 0.2.0 was wrong by 15x.** The load generator was
  a single-process `httpx.AsyncClient` capped at roughly 110 RPS — one twentieth
  of what a single worker can do — so it measured itself, not the server.
  Re-measured with wrk:

  | Config | 0.2.0 (wrong) | Actual |
  |---|---|---|
  | 1 worker | 145 RPS | **2,247 RPS** |
  | 4 worker | 131 RPS, "no improvement" | **6,681 RPS (2.97x)** |
  | 8 worker | untested | **8,068 RPS (3.59x)** |

  The conclusion inverts: multi-worker was always worth it. The bottleneck is the
  GIL, one worker per core, not a lock in this codebase — proven by server CPU
  tracking core count exactly (109% / 407% / 700%) and throughput being flat
  across concurrency levels at one worker.
- `docs/design/CAPACITY.md` opens with a correction notice rather than quietly
  editing the old numbers, because anyone who read 0.2.0 made decisions on them.
- `RUNBOOK.md` previously advised against using multi-worker for throughput.
  That advice was based on the bad measurement and is now reversed, with a
  warning not to trust single-process Python load generators.

### Added

- `examples/quickstart/` — three runnable examples (minimal turn, real endpoint,
  approval flow) and `docs/design/QUICKSTART.md` with a concept table and a list
  of ten onboarding obstacles found by walking the repo as a newcomer.
- `scripts/bench_multiworker.sh` — the wrk harness that produced the corrected
  numbers.
- `scripts/bench_sqlite.py` — write contention under threads and processes.
  Multi-process writes to one file scale to 3.07x with zero failures, so sqlite
  is fine for a single replica; the argument for Redis is fault domain and
  operability, not throughput.

### Measured across machines

Putting the load generator on a separate 64-core host over a 28ms Tailscale link
produced a result worth more than the missing 8-worker ceiling:

| Config | Same host | Across machines |
|---|---|---|
| 1 worker | 2,247 | 2,151 |
| 8 worker | 6,911-8,068 | **3,260** |

One worker agrees, eight workers differ by 2.5x. At one worker the server is the
bottleneck; at eight the network is. Server CPU stayed at 271% of an available
800% while the cross-machine run reported 3,260 RPS, so the server was idle and
the requests were queued on the wire. Raising concurrency from 1,024 to 4,096
did not help — only latency grew.

The deployment lesson is in CAPACITY.md: the same server capability delivers very
different throughput depending on how far away the clients are, so capacity
planning has to start from client location, not from the server's ceiling.

### Not settled

- The 8-worker ceiling on one host is not isolated: the box has 10 cores and the
  load generator competes for them.
- All RPS figures use the built-in scripted model (pure CPU). With a real
  upstream each turn becomes network wait, so concurrency capacity differs,
  though the one-worker-one-core constraint does not.

---

## [0.2.0] - 2026-11-26

Closes the four gaps that 0.1.0 listed as unverified. Two of them were not gaps
in measurement but real defects.

### Added

- **`AGENT_RUNTIME_STORE=redis`** — the only way to share sessions and
  checkpoints across replicas. `RedisStore` had been implemented for a while but
  was never wired into `_make_store()`, so multi-replica deployments could only
  choose `sqlite` or `file`, both single-machine. Verified: `turn_index` stays
  continuous across replicas and Redis holds the full history.
- **`AGENT_RUNTIME_WORKERS`** plus a module-level `app_factory()`. uvicorn only
  accepts an import string for workers and **silently ignores an app object**,
  so the previous launch path could never have run multi-worker even if asked.
- **`AGENT_RUNTIME_TOKEN_PROFILE`** (`calibrated` by default, `conservative`
  available). See Fixed below for why the default matters.
- **`AGENT_RUNTIME_GOVERNANCE_QUOTA`** to configure or disable the governance
  quota, which was previously stuck at its default of 60/min with no override.
- `scripts/soak_test.py` — sustained load with per-minute sampling of RSS,
  threads and file handles. Criterion is the slope of the troughs, not of every
  sample.
- `scripts/real_llm_capacity.py` — upstream latency distribution against a real
  endpoint.

### Fixed

- **Production used the conservative token profile.** `DEFAULT_PROFILE` charges
  1.0 token per CJK character; the measured value is 0.68. `serve.py` used the
  conservative one, so pure Chinese prompts stopped at ~64% of budget, wasting
  36% and triggering compaction early. `CALIBRATED` existed and the docs
  recommended it, but the production path never picked it up.
- **Cache hits were billed at full price.** The adapter parsed
  `prompt_cache_hit_tokens` but `cost_of()` accepted only input and output.
  `Usage` now exposes `total` for the context window and `billable()` for cost;
  `cache_hit_ratio` is a constructor argument because discounts vary by provider.
- **Compaction cut blindly by index**, splitting an assistant turn from its tool
  results. Measured on 2000 random realistic histories: 59.2% produced a tail
  starting with an orphan tool entry. Cuts now step back to a unit boundary.
- **Cross-replica sessions did not work**, as described above.
- Three metrics added because alert rules needed them:
  `agent_runtime_info{rate_limit_distributed}`, `checkpoint_errors_total`,
  `workflow_running`. Four rules in the first draft queried metrics that did not
  exist; three were backed by new metrics and one was deleted rather than
  quietly left in place.

### Measured

| Item | Result |
|---|---|
| Soak, 20 minutes x2 | 121k and 137k requests, 100% success, p99 105-167ms, threads constant at 10, file handles flat, steady-state RSS 85MB with a 238MB peak, returning to 6MB after load stops |
| Real-LLM latency | deepseek-flash p50 2672ms, p99 3805ms per call; a 4-6 step turn is 11-16 seconds |
| Multi-worker | Requests spread across four workers, throughput does not rise; the load generator saturates near 110-130 RPS first |
| Cross-replica sessions | Works with `STORE=redis` |

### Not settled

- **Multi-worker throughput** could not be measured: one client process caps out
  around 110-130 RPS, hiding whatever the server can do. Needs `wrk`/`k6` or a
  second machine. Recorded as an open question, not a result.
- **sqlite write contention** under concurrent load is not quantified.
- **Soak ran 20 minutes.** Slow leaks over hours would not show.

### Changed behaviour

- Production now uses the calibrated token profile. Anyone relying on the old
  over-estimate should set `AGENT_RUNTIME_TOKEN_PROFILE=conservative`.
- `Usage.total` is documented as the window view; billing must use
  `billable()`. Code that priced tokens from `total` will now disagree with the
  adapter's own cost figure.
- The repository is now `XiaoBinGan/Turing_AgentRuntime`; the Pages URL moved to
  `/Turing_AgentRuntime/` and the old one 404s.

---

## [0.1.0] - 2026-11-25

First public release. The engine is complete and heavily tested; the
operational surface is deliberately not, and that is documented rather than
hidden.

### Added

**Core control plane** (`core/`)

- `Gateway`: eleven-step admission chain — shape validation, principal
  resolution, RBAC, input guard, quota, context resolution, loop dispatch,
  audit, checkpoint, unified exit. Fails closed on every uncertain decision.
- `Loop`: bounded state machine with six termination conditions (done, budget,
  deadline, deadlock, unrecoverable, needs-confirmation) and three deadlock
  detectors (iteration count, semantic fingerprint, arbitration).
- `ContextHandle`: patch-protocol state. Every write is
  `apply(Patch(op, path, by, value))` and `OWNER_TABLE` decides whether that
  owner may touch that field. The patch log doubles as trajectory, replay
  source, and write-ahead log.
- `StreamingGateway`: tails the patch log and emits incremental SSE frames
  without re-serializing the whole result.
- `store`: context/session/checkpoint persistence behind one SPI, with
  `memory`, `file`, and `sqlite` implementations.
- `session`: multi-turn conversation carry-over with TTL and size caps.
- `observability`: metrics, structured logging, shutdown coordination.
  `prometheus_client` is optional; absent it raises clearly instead of
  returning empty metrics.

**Resource planes** (`plane/`)

- `tool`: manifest-driven dispatch with risk tiers (T0-T3), approval flow,
  secret injection that never reaches results, idempotency cache, per-turn
  limits, and timeouts.
- `memory`: extraction, dedupe by fact key, recall with scoring.
- `knowledge`: RAG with pluggable vector backends (`memory`, `qdrant`,
  `milvus`), SQLite FTS5 full-text index, and pluggable splitters
  (`heading`, `llama-index`).
- `dag` / `workflow`: static DAG with layer barriers, `SubagentRunner` with
  four-layer privilege reduction, hierarchical orchestration, saga
  compensation, envelope bus.
- `governance`: RBAC, input/output guards, quota.
- `quality`: evaluation and critique, fail-open by design.
- `sandbox_backends` / `sandbox_tool`: isolation backends behind one SPI.

**HTTP surface** (`serve.py`)

- `/v1/turns` and `/v1/turns/stream` (SSE) with auth, rate limiting, and
  implicit deadline handling.
- `/v1/workflows` endpoints: submit, query, list, resume, and a progress
  stream. Workflow state persists; unfinished work is marked `interrupted`
  rather than silently replayed.
- `/healthz`, `/readyz`, `/metrics`, `/v1/info` for operations.

**Capabilities added during hardening**

- Parallel tool execution within a turn, with a T2 fallback to serial so an
  approval point cannot be executed past.
- Sub-agent progress channel (`ExecCtx.progress`) so nested work is visible
  over SSE without polluting the context patch log.
- Side-effect checkpointing: a checkpoint is written after every successful
  non-read-only tool call, so a crash leaves a record of which writes already
  happened.
- Engine mutual exclusion (`ContextHandle.bind_engine`) and an
  `Owner.WORKFLOW` state field so DAG execution is replayable.

### Verified

- 2215 unit and integration tests, stable across repeated full runs
- 11 end-to-end scenarios, 80 assertions
- 100-thread concurrency against one gateway; zero exceptions
- Memory bounded: 900 turns grow 3KB with a capped audit log
- Rate limiting exact under concurrency: 40 concurrent, limit 10, exactly 10 pass
- Real model convergence in 4-6 steps on a root-cause task
- Real Milvus cluster, real Langfuse ingestion, real OTLP delivery
- Graceful shutdown ordering; checkpoint crash recovery; workflow persistence

### Operational surface (verified)

- Multi-replica: two replicas on a shared Redis, tenant quota exact across both.
  `scripts/verify_multi_replica.sh`
- Capacity baseline: single replica 145 RPS plateau, 2 replicas 192 RPS.
  `scripts/loadtest.py`, `docs/design/CAPACITY.md`
- Backup and restore: sqlite via the backup API, file store by directory,
  corrupted payloads refused. `scripts/verify_backup_restore.sh`
- Schema migration: versioned envelope migrations applied stepwise; unknown
  and future versions refused. `tests/test_store_migration.py`
- SLO and alerting: six SLIs with targets from the measured baseline, nine
  rules validated by promtool. `docs/design/SLO.md`, `deploy/prometheus/`
- On-call runbook: per alert, how to confirm, what to do first, what not to do.
  `docs/design/RUNBOOK.md`
- Chaos drill: KV down, store read-only, upstream unreachable, client abort.
  `scripts/chaos_drill.sh`

### Not verified

- Long-running soak: the load test runs 12 seconds, not an hour
- Real-LLM capacity: upstream latency dominates and would change every number
- Multi-worker: `uvicorn --workers N` to work around the GIL is untested
- Cross-replica sessions: session state lives in each replica's own store

### Known limitations

- `serve.py` is a single 239KB module. It holds the complete admission chain,
  where order is semantics. Splitting it by line count would make that order
  harder to verify; it should be split by concern when someone actually needs
  to change it.
- Workflow DAG definitions are not persisted. After a restart, `resume`
  returns 409 with an explanation rather than pretending it can continue.
- Workflow capacity rejects with 429 when full instead of queueing. A queue
  would also need to be bounded, which only relocates the memory problem.
- Workflow nodes run as the submitter's turn, so INV-18 privilege reduction
  applies to sub-agents (C path) but not to DAG nodes (B path). Downgrading
  DAG nodes to read-only would make a deploy pipeline unable to deploy.

### Fixed

- `ToolManifest.idempotent` defaulted to `True`, which meant a `MUTATING`
  tool constructed in code without an explicit flag would be cached: the same
  arguments on retry returned the first result without executing. Declarative
  config already derived the right value, so the two paths disagreed and the
  code path was the dangerous one. Now `bool | None`, derived by
  `cacheable()`; explicit `False` overrides everything.
- `per_turn_limit` never fired for cached tools, because the cache check ran
  first and returned before counting. A tool limited to 2 calls accepted 4.
  Rate limiting now counts calls; budget still counts real executions.
- `Dispatcher` replacement lost approval, timeout, budget accounting,
  idempotency and T3 refusal silently. `capabilities()` plus
  `Gateway.check_dispatch()` turns that into a startup failure.
- `Loop._manifest()` failed when the dispatcher exposed `manifests()` as a
  method rather than an attribute, so side-effect checkpointing treated every
  tool as unknown and checkpointed read-only calls too.

Found while building the operational surface:

- Tenant quota was per-process. `AGENT_RATE_LIMIT_DISTRIBUTED` only affected the
  HTTP per-IP layer, so a Gateway quota of 60 allowed 300 across five replicas.
  `Gateway(quota_counter=...)` now does atomic increments against shared KV.
- `RESPKV` connects lazily, so `info().shared` was always true and a
  misconfigured distributed setup started successfully and failed on the first
  request. Startup now probes the KV when distribution is required.
- `FileStore` created its root directory without checking writability, so a
  read-only mount started cleanly and failed at the first checkpoint, possibly
  after a tool with side effects had already run.
- `QuotaGuard` had no way to express "unlimited", diverging from the
  `AGENT_RATE_LIMIT=0` convention. Load testing was blocked by the default
  60/60s with only "quota exceeded" to go on.
- `schema_version` mismatches made all stored payloads unreadable. There is now
  a migration table applied one version at a time.

### Fixed after a cross-project audit

An issue list from a sibling TypeScript implementation prompted a self-audit.
Four issues applied here:

- **CJK token estimate overestimated by 1.57x in production.** `DEFAULT_PROFILE`
  is deliberately conservative (1.0 token per CJK character) and `CALIBRATED`
  holds the measured 0.68, but `serve.py` used the conservative one. Pure
  Chinese prompts stopped at ~64% of the budget, wasting 36% and triggering
  compaction early. Production now defaults to calibrated; the conservative
  profile is available via `AGENT_RUNTIME_TOKEN_PROFILE=conservative` for
  models that have not been measured.
- **Cache hits were billed at full price.** The adapter parsed
  `prompt_cache_hit_tokens` into `Usage.cache_hit_tokens` but `cost_of()`
  only took input and output, so cached tokens were charged fully. Usage now
  exposes two explicit views: `total` for the context window (cache hits do
  occupy it) and `billable()` for cost (cache hits are discounted).
  `cache_hit_ratio` is a constructor parameter because the discount varies by
  provider.
- **Compaction cut blindly by index.** `HistoryCompressor` sliced at
  `len(history) - max_keep`, which splits an assistant turn from its tool
  results. Measured on 2000 random realistic histories: 59.2% produced a tail
  starting with an orphan tool entry. Cuts now step back to a unit boundary.
- **A new gate checks documentation against default values.**
  `scripts/check_config_defaults.py` asserts that the profile production
  actually uses matches the documented one, that the conservative switch really
  works, and that every `AGENT_RUNTIME_*` name mentioned in the docs exists in
  `serve.py`. The existing README gate could catch a stale module count but not
  a reversed default, which is what the first issue was.

### Tooling

- `verify.sh`: six gates (compile, tests, contract self-consistency, README
  facts, end-to-end scenarios, HTTP surface).
- `scripts/preflight_oss.sh`: pre-publication audit for secrets, private keys,
  internal addresses, oversized files, and internal document leakage.
- `scripts/scan_secrets.py`: heuristic credential scan that does not flag test
  fixtures or the redaction regexes themselves.
- `scripts/check_readme_facts.py`: compares README claims with the code so
  documentation cannot rot silently.

[0.7.1]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.7.1
[0.7.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.7.0
[0.6.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.6.0
[0.5.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.5.0
[0.4.1]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.4.1
[0.4.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.4.0
[0.3.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.3.0
[0.2.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.2.0
[0.1.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.1.0
