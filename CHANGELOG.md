# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this is a runtime skeleton rather than a library, `0.x` versions may
change contracts. Any change to `contracts.py` will be called out explicitly
under **Breaking**, with a migration note.

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

[0.2.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.2.0
[0.1.0]: https://github.com/XiaoBinGan/Turing_AgentRuntime/releases/tag/v0.1.0
