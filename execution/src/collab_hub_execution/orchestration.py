"""The engine contract, and the names callers have imported from here.

The lifecycle lives in ``LifecycleRunner`` (``runner.py``), which implements the
``WorkflowEngine`` contract and runs on the durability backend its configuration
names (``backends/``). The Op, its steps and the seam's types are in ``ops.py``.
``DurableWorkflowEngine``, the reference engine that recovered runs from the
Track, is gone with that recovery (ADR-0002 D2): a run a stopped host left
unfinished is ``interrupted``, and continues only through ``retry()``. Everything
this module used to define is re-exported here for callers that import it from it.
"""

from __future__ import annotations

from .claims import key_component as _key_component  # noqa: F401 - re-exported for callers that import it from here
from .ops import (  # noqa: F401 - re-exported for callers that import them from here
    _NO_SIGNAL,
    CogExecutor,
    CogWorker,
    InMemoryCogExecutor,
    OpDefinition,
    OpStep,
    WorkflowEngine,
    _canonical_op,
    _deserialize_op,
    _serialize_op,
)
from .runner import (  # noqa: F401 - re-exported for callers that import them from here
    MESSAGE_MAX_CHARS,
    STEP_FUNCTIONS,
    LifecycleRunner,
    UsageUnavailable,
)
