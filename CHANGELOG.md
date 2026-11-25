# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this is a runtime skeleton rather than a library, `0.x` versions may
change contracts. Any change to `contracts.py` will be called out explicitly
under **Breaking**, with a migration note.

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

- 2187 unit and integration tests, stable across repeated full runs
- 11 end-to-end scenarios, 80 assertions
- 100-thread concurrency against one gateway; zero exceptions
- Memory bounded: 900 turns grow 3KB with a capped audit log
- Rate limiting exact under concurrency: 40 concurrent, limit 10, exactly 10 pass
- Real model convergence in 4-6 steps on a root-cause task
- Real Milvus cluster, real Langfuse ingestion, real OTLP delivery
- Graceful shutdown ordering; checkpoint crash recovery; workflow persistence

### Not verified

Stated plainly because an enterprise decision depends on it:

- Multi-replica deployment against a shared Redis has never been exercised
- No load test: no p50/p95/p99 under sustained QPS
- No backup/restore drill; no schema migration path for stored payloads
- No SLO definitions, alerting rules, or on-call runbook
- No chaos engineering catalogue

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

### Tooling

- `verify.sh`: six gates (compile, tests, contract self-consistency, README
  facts, end-to-end scenarios, HTTP surface).
- `scripts/preflight_oss.sh`: pre-publication audit for secrets, private keys,
  internal addresses, oversized files, and internal document leakage.
- `scripts/scan_secrets.py`: heuristic credential scan that does not flag test
  fixtures or the redaction regexes themselves.
- `scripts/check_readme_facts.py`: compares README claims with the code so
  documentation cannot rot silently.

[0.1.0]: https://github.com/XiaoBinGan/agent-runtime/releases/tag/v0.1.0
