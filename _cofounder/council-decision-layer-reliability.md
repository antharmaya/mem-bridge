# Council Decision Layer — Module C: Reliability & Feedback Mechanics

> **Status:** Spec complete — ready for implementation.
> **Date:** 2026-06-21
> **Author:** Hermes (Council Decision Layer sprint)
> **Target Score:** 9/10

---

## Overview

Module C provides the reliability layer for the Council Decision Layer. It
ensures that:

1. Every decision outcome is measurable and traceable
2. The framework learns from past decisions via outcome feedback
3. Confidence calibration improves over time
4. The system can audit its own decision quality

This document specifies the concrete implementation plan for Module C:
trajectory log schema, outcome measurement triggers, and feedback loop
mechanics.

---

## 1. Trajectory Log Schema

Every decision goes through a lifecycle:
`consideration → decision → action → outcome → review`

### Table: `decision_trajectory`

```sql
CREATE TABLE IF NOT EXISTS decision_trajectory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Link to structured decision
    decision_id TEXT NOT NULL,                    -- FK to structured_decisions.content_hash
    source_agent TEXT NOT NULL,                   -- which agent made this
    session_id TEXT,                              -- current session context

    -- Lifecycle stages (timestamps)
    considered_at TEXT,                           -- when first detected
    decided_at TEXT,                              -- when decision was formalized
    actioned_at TEXT,                             -- when action was taken
    outcome_checked_at TEXT,                      -- when outcome was verified

    -- Stage state machine
    stage TEXT NOT NULL DEFAULT 'considered'      -- considered|decided|actioned|verified|archived
        CHECK (stage IN ('considered', 'decided', 'actioned', 'verified', 'archived')),

    -- Context snapshot (JSON)
    context_snapshot TEXT,                        -- what was happening when decision was made
    alternatives_json TEXT,                       -- alternatives considered at decision time
    expected_outcome TEXT,                        -- what the decision-maker expected to happen

    -- Actual outcome (populated at verification time)
    actual_outcome TEXT,                          -- what actually happened
    outcome_satisfaction INTEGER,                 -- 1-5 Likert scale
    outcome_evidence TEXT,                        -- supporting evidence (log snippets, etc.)

    -- Metadata
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trajectory_decision ON decision_trajectory(decision_id);
CREATE INDEX IF NOT EXISTS idx_trajectory_stage ON decision_trajectory(stage);
CREATE INDEX IF NOT EXISTS idx_trajectory_outcome ON decision_trajectory(outcome_checked_at);
```

### Stage Machine Rules

| From → To | Trigger | Condition |
|-----------|---------|-----------|
| considered → decided | Human/agent confirms decision | Pattern score ≥4 |
| decided → actioned | Tool call or deployment detected | Match against known action patterns |
| actioned → verified | Time passes OR manual check | Default: 14-day interval, configurable |
| verified → archived | Review completed | outcome_satisfaction recorded |
| * → archived | Manual archive | Explicit user request |

### Decision Quality Score

Derived from trajectory completeness:

```
quality_score = (
    (rationale_completeness * 0.25) +
    (outcome_verification * 0.30) +
    (confidence_calibration * 0.25) +
    (traceability * 0.20)
)
```

Where:
- `rationale_completeness`: 0.0–1.0 (was rationale populated? alternatives?)
- `outcome_verification`: 0.0–1.0 (was outcome checked? evidence attached?)
- `confidence_calibration`: 0.0–1.0 (was confidence close to outcome truth?)
- `traceability`: 0.0–1.0 (can we reconstruct the context?)

---

## 2. Outcome Measurement Triggers

### Trigger A: Time-Based Review

```
Cron: every 7 days (configurable per skill)
Action: SELECT decisions WHERE outcome_checked_at > 14 days OR outcome_verified = 0
        → present review prompt to user
```

### Trigger B: Deployment Detection

When a tool call or deployment event is detected that relates to a pending
decision, auto-move to check:

```
Event: tool call with name matching decision keywords
Action: Move trajectory to 'actioned' stage
        Schedule: check outcome in 48 hours
```

### Trigger C: Manual Verification

User-facing command:

```bash
hermes decision verify <decision_id> --outcome satisfied --notes "It worked!"
```

Or via the `mb_decision_review` tool:

```
Tool: mb_decision_review.mark_verified(decision_id, outcome, notes)
```

### Trigger D: Session End Correlation

When a session ends containing a decision, prompt:

> "You decided [X] in this session. Want to set a reminder to check if it worked?"

---

## 3. Feedback Loop Mechanics

### Loop 1: Pattern Weight Auto-Adjustment

If a pattern frequently triggers false positives (decision detected but no
decision actually made), auto-downgrade its weight:

```python
def auto_adjust_weights(self):
    """Reduce weight for patterns that produce false positives."""
    # Track: pattern_name -> {true_positives: int, false_positives: int}
    # If false_positive_rate > 30% over 10+ triggers:
    #   pattern_weight = max(1, pattern_weight - 1)
    # If true_positive_rate > 80% and weight < max_suggested:
    #   pattern_weight = min(max_suggested, pattern_weight + 1)
    pass
```

### Loop 2: Confidence Calibration

Compare extraction confidence vs outcome satisfaction:

```python
def calibrate_confidence(self):
    """Adjust confidence based on outcome tracking."""
    # For decisions with verified outcomes:
    #   If confidence was high but outcome was bad → reduce conf for similar patterns
    #   If confidence was low but outcome was good → increase conf for similar patterns
    # Store calibration_offset per pattern
    pass
```

### Loop 3: Framework Usage Analytics

Track which frameworks produce reliable decisions:

```sql
CREATE TABLE IF NOT EXISTS framework_analytics (
    framework_name TEXT PRIMARY KEY,
    total_decisions INTEGER DEFAULT 0,
    verified_outcomes INTEGER DEFAULT 0,
    avg_satisfaction REAL DEFAULT 0.0,
    pattern_precision REAL DEFAULT 0.0,
    last_used TEXT
);
```

### Loop 4: Nudge/Review Cycle

```
Weekly cadence:
1. Query: decisions older than 14 days, outcome_verified = 0
2. Present in system prompt prefetch block:
   "📋 Pending decision reviews: {count} decisions awaiting outcome verification"
3. If user engages: show oldest unverified decisions first
4. Record outcome → update trajectory → calibrate confidence
```

### Loop 5: Audit Trail

Every change to a structured decision or trajectory is logged:

```sql
CREATE TABLE IF NOT EXISTS decision_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    field_changed TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT NOT NULL,        -- 'agent' or 'human' or 'system'
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_decision ON decision_audit_log(decision_id);
```

---

## 4. Integration Points

### Integration with Memory Bridge

| Component | Integration | Priority |
|-----------|-------------|----------|
| structured_decisions | Trajectory `decision_id` links to `content_hash` | P0 |
| pattern_detector | Pattern weights fed by feedback loop | P0 |
| memory_provider | Prefetch block shows pending reviews | P1 |
| system_prompt_block | Decision review nudge | P1 |

### Integration with Hermes Agent

| Component | Integration | Priority |
|-----------|-------------|----------|
| question_engine.py | `ask_structured_question` logs to decision_trajectory | P0 |
| skill patterns | Trajectory linked to framework_used | P0 |
| cron | Weekly decision review cron | P1 |

---

## 5. Testing Plan

```python
# tests/test_decision_trajectory.py

def test_trajectory_lifecycle():
    """Full lifecycle: considered → decided → actioned → verified → archived."""
    ...

def test_outcome_measurement_trigger_time_based():
    """Time-based trigger fires correctly."""
    ...

def test_feedback_loop_pattern_adjustment():
    """Auto-adjustment reduces weight for false-positive patterns."""
    ...

def test_audit_log_created_on_state_change():
    """Every trajectory state change creates an audit entry."""
    ...
```

---

## 6. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| False positive patterns degrade trust | Auto-weight adjustment + human override |
| Outcome verification fatigue | Cap pending reviews to 3 per session |
| Trajectory table bloat | Auto-archive decisions > 90 days old |
| Audit log grows unbounded | Rotate logs after 1 year, archive to compressed file |

---

## 7. Future Enhancements (Post-launch)

- **ML-based pattern learning**: Train a lightweight classifier on verified
  decisions to improve pattern detection (requires labeled dataset)
- **Cross-agent outcome sharing**: If two agents independently check the same
  decision's outcome, correlate and aggregate
- **Confidence calibration dashboard**: Visual analytics for decision quality
  over time
- **External outcome webhook**: POST decision outcome to a URL (for CI/CD
  integration)
