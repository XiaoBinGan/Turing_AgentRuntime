# Contributing

Read this before opening a pull request. The rules below are not style
preferences; each one exists because breaking it caused a real defect that the
test suite had to catch after the fact.

## The one rule that matters

**`contracts.py` is frozen.**

It defines every shared type and every state ownership rule. If you need a new
shared type, define it in your own module. If you genuinely must extend the
contract layer, the pull request needs:

1. A migration note: what happens to state persisted under the old contract.
2. A test that fails before your change and passes after.
3. A paragraph in the pull request explaining why the existing protocol could
   not be satisfied.

Anything else gets closed.

## Boundaries the architecture test enforces

`tests/test_architecture.py` parses the AST of every module. It fails the build
on these:

| Rule | Why it exists |
|---|---|
| No module in `core/` imports a third-party package | The core runtime must run on the standard library alone. This is what makes the zero-dependency install real. |
| No runtime module imports `agents/` | The runtime owns shape; the application owns content. A static import is how domain vocabulary leaks in. |
| No CJK string literals in `builder.py` | Prompt assembly must stay language-neutral. Localized wording belongs to templates, not code. |
| `serve.py`, `builder.py`, `toolconfig.py` never import `agents` | Same reason, at the HTTP edge. Extensions load through `load_extension("module:attr")`. |

## Adding an extension

Extensions are discovered by dotted path, never imported statically:

```
AGENT_RUNTIME_EXTRACTOR=agents.extractor:OpsExtractor
AGENT_RUNTIME_ROUTER=agents.registry:build_router
```

If your implementation needs constructor arguments, expose it as a **factory
function** rather than an instance. The loader calls what it finds.

## Tests

```bash
./verify.sh                                    # the five gates
.venv/bin/python -m unittest tests.test_x -v   # one module
```

Requirements for any pull request that changes behavior:

- A test that fails without the change. Describe in a comment what defect it
  catches, not what the function does.
- If the change touches concurrency, memory, or resource limits, add a test that
  would catch the regression **under load**, not just the happy path.

We use `unittest`, not pytest. Comments in code are written in Chinese;
identifiers are English. That is deliberate: the design discussions happened in
Chinese and the code is read internationally.

## What we will not merge

- A new mandatory dependency in `core/`.
- A feature that makes a limit unbounded. Every resource that a single request
  can amplify must have a cap. Node counts, budgets, page sizes, metric labels,
  queue depths, workflow capacity — all of them.
- Mechanics that reject duplicate tool calls. It looks like a safety win and
  breaks three other things: the idempotency cache, the approval `args_hash`,
  and sub-agent depth accounting.
- Silent failure handling. If something cannot be persisted, logged, or
  evaluated, it must be visible in the result, not swallowed.

## Reporting a bug

Include the failure mode, not just the symptom. "Tool call returns 200 but the
result is empty" is a symptom. "The dispatcher's budget counter is incremented
before the handler runs, so a denied call still consumes quota" is a report we
can act on.

If you can, include the smallest reproduction that fails. A failing test case is
worth more than a paragraph.

## Commit messages

Imperative mood, one line under 72 characters, and a body that explains the
failure mode being fixed. Reference the invariant if there is one (`INV-18`).

```
Fix per-turn limit bypass under parallel tool execution

The dispatcher incremented _turn_calls outside any lock, so concurrent
invocations read the same `used` value and the limit was bypassed.
Measured: limit=10 accepted 40 calls.
```
