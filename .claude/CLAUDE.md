## Async Python patterns

### TaskGroup over asyncio.gather

Prefer `asyncio.TaskGroup` for concurrent operations. TaskGroups provide structured
concurrency with proper exception propagation. Use `asyncio.gather` only when you
need partial success handling (`return_exceptions=True`) or must inspect all
exceptions individually.

```python
async with asyncio.TaskGroup() as tg:
    tree_task = tg.create_task(build_tree(...))
    assets_task = tg.create_task(extract_assets(...))

tree = tree_task.result()
assets = assets_task.result()
```

### TaskGroup with Semaphore for bounded concurrency

```python
semaphore = asyncio.Semaphore(5)

async def bounded_call(item):
    async with semaphore:
        return await process(item)

async with asyncio.TaskGroup() as tg:
    tasks = [tg.create_task(bounded_call(i)) for i in items]

results = [t.result() for t in tasks]
```

### Session scoping

Keep results within the async context block — never let a session-scoped
resource escape its `async with` block.

---

## Temporal workflow patterns

### Folder structure

```
prod/workflows/<workflow_name>/
├── workflow.py             # Workflow definition
├── models.py               # Input/output Pydantic models
├── activities/
│   ├── __init__.py         # Exports: activities = [...]
│   ├── stage_one.py
│   └── stage_two.py
└── utils/                  # Pure utilities (optional)
```

Simple workflows use a single `activities.py` instead of the folder.

### imports_passed_through

Keep `workflow.unsafe.imports_passed_through()` at the top of workflow modules
so activities and models import correctly inside the workflow sandbox.

```python
with workflow.unsafe.imports_passed_through():
    from .activities.build_tree import build_tree_activity
    from .models import ProcessPdfWorkflowInput
```

### Activity design

Activities are single-purpose units with Pydantic input/output models. All
inputs and outputs must be serializable. Pass URIs, not payloads — anything
larger than a few KB goes through the artifact store.

```python
@activity.defn(name="process-pdf_build-tree")
async def build_tree_activity(input: BuildTreeInput) -> BuildTreeOutput:
    ...

activities = [build_tree_activity, extract_assets_activity]
```

### Activity input/output models

Every activity gets a frozen Pydantic model for input and output. Use
`model_config = ConfigDict(frozen=True)` for immutability.

```python
class BuildTreeInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    pdf_path: str
    document_id: str
    run_id: str

class BuildTreeOutput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tree_uri: str
    node_count: int
    total_pages: int
```

### Retry policies

Shared constants for consistency:

```python
DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=3,
)
NO_RETRY_POLICY = RetryPolicy(maximum_attempts=1)
```

Use `ApplicationError(msg, non_retryable=True)` for errors where retrying
won't help (validation failures, missing config, business logic errors).

### Timeout conventions

- Database/cache lookups: 15–30 seconds
- API calls with retries: 1–5 minutes
- Long sync operations: 10+ minutes
- GPU model inference: 30+ minutes

### Workflow ID strategies

Use predictable, deterministic IDs based on the resource:

```
process-pdf-{document_id}-{run_id}
```

ID conflict policies:
- `TERMINATE_EXISTING`: replace running workflow (only latest matters)
- `USE_EXISTING`: reuse running workflow (idempotent triggers)
- `FAIL`: reject if exists (use with signal-based enqueueing)

### continue_as_new

Use `continue_as_new` for workflows that process items in batches to prevent
hitting the 50MB event history limit. Check with
`workflow.info().is_continue_as_new_suggested()`.

### Task queues

Separate task queues isolate workloads and enable independent scaling:

```
cpu-task-queue   — PyMuPDF, tree, regex, formatting, S3 IO
gpu-task-queue   — text LLM + chandra inference
```

### Concurrency in Temporal

Concurrency caps belong on the Temporal worker, not in Python semaphores:

```python
Worker(
    client,
    task_queue="gpu-task-queue",
    workflows=[ProcessPdfWorkflow],
    activities=gpu_activities,
    max_concurrent_activities=4,
)
```

### Error handling in activities

Use `ApplicationError` with `non_retryable=True` for non-transient failures:

```python
if not connection:
    raise ApplicationError(
        f"Connection {connection_id} not found",
        non_retryable=True,
    )
```

### Pydantic conventions

- `extra="forbid"` for internal models (catches typos)
- `extra="ignore"` for external API response models
- `frozen=True` for all workflow/activity I/O models
- Use `@field_validator` with `@classmethod` for field-level validation
- Use `@model_validator(mode="after")` for cross-field validation
- Use discriminated unions for polymorphic types

---

## Agent skills

### Issue tracker

GitHub Issues (mcnojo/ocr-benchmarking). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout. See `docs/agents/domain.md`.
