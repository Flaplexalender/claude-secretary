# Deterministic Pipelines Implementation Plan

## Phase 1: Determinism Audit + Router Freeze (Week 1)

### Files to Create
1. `src/secretary/deterministic_pipeline.py` — Pipeline DSL + executor
2. `tests/test_deterministic_pipelines.py` — Golden output tests

### Files to Modify
1. `src/secretary/router.py` — Add seed parameter, freeze model selection
2. `src/secretary/direct_agent.py` — Add determinism mode flag

### Step 1: Audit Non-Determinism (15 min)
```bash
grep -rn "random\|uuid\|datetime\.now\|time\.time" src/secretary/router.py
grep -rn "random\|uuid\|datetime\.now\|time\.time" src/secretary/direct_agent.py
```

**Expected findings:**
- `router.select_model()` → no RNG (deterministic ✓)
- `direct_agent.run()` → timestamp logging (fix: use checkpoint ts)
- Tool calls → order may vary (fix: sort tool IDs)

### Step 2: Router Freeze (30 min)
```python
# src/secretary/router.py addition

@dataclass
class RoutingDecision:
    model: str
    tier: str
    reasoning: str
    seed: Optional[int] = None  # NEW: for reproducibility

def select_model(
    task: str,
    complexity: float,
    seed: Optional[int] = None,
) -> RoutingDecision:
    """Deterministic model selection with optional seed override."""
    if seed is not None:
        random.seed(seed)  # Freeze RNG if seed provided
    
    # Existing logic is already deterministic (rule-based)
    # Just add logging of seed used
    decision = RoutingDecision(
        model=model,
        tier=tier,
        reasoning=reasoning,
        seed=seed,
    )
    return decision
```

### Step 3: Pipeline DSL (45 min)
```python
# src/secretary/deterministic_pipeline.py

from dataclasses import dataclass
from typing import Any, Callable
import yaml

@dataclass
class PipelineStep:
    id: str
    action: str  # "run_agent", "route", "branch"
    params: dict[str, Any]
    retries: int = 3
    on_failure: str = "stop"  # "stop", "continue", "fallback"

@dataclass
class Pipeline:
    name: str
    seed: int
    steps: list[PipelineStep]
    
    @classmethod
    def from_yaml(cls, path: str) -> "Pipeline":
        with open(path) as f:
            config = yaml.safe_load(f)
        return cls(
            name=config["name"],
            seed=config.get("seed", 42),
            steps=[PipelineStep(**s) for s in config["steps"]],
        )

class DeterministicExecutor:
    def __init__(self, agent, memory):
        self.agent = agent
        self.memory = memory
        self.execution_log = []
    
    async def execute(self, pipeline: Pipeline) -> dict[str, Any]:
        """Run pipeline with snapshot logging."""
        snapshot = {
            "pipeline": pipeline.name,
            "seed": pipeline.seed,
            "steps_executed": [],
            "final_output": None,
        }
        
        state = {}
        for step in pipeline.steps:
            log_entry = {
                "step_id": step.id,
                "action": step.action,
                "input": state,
            }
            
            if step.action == "run_agent":
                result = await self.agent.run(
                    task=step.params["task"],
                    seed=pipeline.seed,
                )
                state.update(result)
            
            elif step.action == "branch":
                condition = step.params["condition"]
                if condition(state):
                    state["next_step"] = step.params["if_true"]
                else:
                    state["next_step"] = step.params["if_false"]
            
            log_entry["output"] = state
            snapshot["steps_executed"].append(log_entry)
        
        snapshot["final_output"] = state
        await self.memory.store_pipeline_snapshot(snapshot)
        return snapshot
```

### Step 4: Test with Golden Outputs (30 min)
```python
# tests/test_deterministic_pipelines.py

@pytest.mark.asyncio
async def test_pipeline_determinism():
    """Same input + seed → same output."""
    pipeline = Pipeline.from_yaml("tests/fixtures/sample_pipeline.yaml")
    
    # Run 1
    result1 = await executor.execute(pipeline)
    
    # Run 2 (reload pipeline, same seed)
    result2 = await executor.execute(pipeline)
    
    # Assert outputs match
    assert result1["final_output"] == result2["final_output"]
    assert result1["steps_executed"] == result2["steps_executed"]

@pytest.mark.asyncio
async def test_pipeline_replay():
    """Replay a stored snapshot produces identical execution."""
    snapshot = await memory.load_pipeline_snapshot("snapshot-uuid")
    
    # Re-execute with stored seed
    result = await executor.execute(
        Pipeline(
            name=snapshot["pipeline"],
            seed=snapshot["seed"],
            steps=snapshot["steps_as_yaml"]
        )
    )
    
    # Assert matches original snapshot
    assert result == snapshot
```

### Step 5: Pipeline YAML Schema (15 min)
```yaml
# tests/fixtures/sample_pipeline.yaml
name: "email_classifier"
seed: 42
steps:
  - id: "step_1"
    action: "run_agent"
    params:
      task: "Classify this email: {email_body}"
    retries: 3
  
  - id: "step_2"
    action: "branch"
    params:
      condition: "output.classification == 'urgent'"
      if_true: "step_3_escalate"
      if_false: "step_4_archive"
  
  - id: "step_3_escalate"
    action: "run_agent"
    params:
      task: "Send alert: {email_sender}"
  
  - id: "step_4_archive"
    action: "run_agent"
    params:
      task: "Store in archive"
```

## Phase 2: Advanced Features (Week 2)

### DAG Visualization
```python
def to_graphviz(pipeline: Pipeline) -> str:
    """Generate DOT format for visualization."""
    lines = ["digraph {"]
    for step in pipeline.steps:
        lines.append(f'  "{step.id}" [label="{step.action}"];')
    lines.append("}")
    return "\n".join(lines)
```

### Distributed Execution (Task Queue)
```python
# Use Celery/RQ to run steps on workers
# Requires: task serialization (JSON) + state synchronization
```

## Reproducibility Guarantees

| Component | Deterministic? | Method |
|-----------|---|---|
| Model selection | ✅ Yes | Rule-based routing |
| Tool order | ✅ Yes | Sort by tool_use_id |
| Timestamps | ✅ Yes | Use checkpoint ts |
| RNG calls | ✅ Yes | Seed-based (frozen) |
| File I/O | ✅ Yes | Checksum validation |

## Success Criteria

- [ ] 10+ golden test cases pass
- [ ] Pipeline snapshot stored + replay identical
- [ ] Audit shows 0 non-deterministic calls in hot path
- [ ] DAG visualization works
- [ ] Documentation complete
