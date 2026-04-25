# OS_Agent v4 Borrowing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Borrow the highest-value ideas from LinuxAgent v4 and integrate them into the current OS_Agent codebase in ways that directly improve hackathon scoring on safety, orchestration, validation, and engineering quality.

**Architecture:** Keep the current CLI-first OS_Agent structure and shared-task orchestrator as the delivery surface, but strengthen four areas: hard safety classification and HITL, explicit workflow/state transitions, config and secret validation, and scenario-based verification. Avoid a full rewrite into the upstream v4 package layout; instead, absorb its strongest patterns into the current repo incrementally.

**Tech Stack:** Python 3, current `src/service` task orchestrator, current CLI UI, unittest, markdown reports, YAML for scenario harness, optional Pydantic v2 for config hardening.

---

## Why these v4 ideas are worth borrowing

- Upstream v4 emphasizes token-aware safety classification, mandatory HITL, append-only audit logs, and stronger engineering gates. Those map directly to the hackathon scoring dimensions for risk control, explainability, and reproducibility.
- The current OS_Agent already has a useful base: `clarify / confirm / execute / reflect / recover`, structured task plans, and filesystem audit artifacts. That means we should extend rather than replace.
- The fastest path to visible improvement is to make decisions more explicit, more testable, and more reportable.

## Target outcomes

1. **Safety becomes stricter and more explainable**
   - Replace broad regex-only gating with token-aware command safety classification inspired by v4.
   - Make every dangerous step produce a machine-readable safety reason and a user-readable explanation.

2. **Workflow becomes stateful and reviewable**
   - Turn the current implicit `plan -> clarify -> execute -> reflect -> recover` path into explicit task states and transitions.

3. **Configuration becomes safer and fail-fast**
   - Borrow v4’s “validate before run” mindset so secrets, paths, and permissions fail fast instead of failing mid-run.

4. **Validation becomes scenario-driven**
   - Add a reusable scenario harness for hackathon scenes, so demo coverage is executable and repeatable instead of doc-only.

---

## Implementation checklist

### Task 1: Borrow v4-style hard safety classification

**Why**

The strongest thing in upstream v4 is not packaging; it is the stricter safety model. This is the most important improvement for hackathon section 2.2.

**Files**

- Modify: `src/service/risk.py`
- Modify: `src/service/models.py`
- Modify: `src/service/orchestrator.py`
- Modify: `tests/test_task_service.py`
- Reference: upstream README sections on `SAFE / CONFIRM / BLOCK`

**What to add**

- [ ] Introduce an explicit safety classification pipeline that distinguishes:
  - `safe`
  - `confirm`
  - `block`
- [ ] Add token-aware command parsing before classification.
- [ ] Separate:
  - destructive file operations
  - permission/user changes
  - service/package management
  - observational diagnostics
- [ ] Preserve the current explanation field, but make the explanation source-specific:
  - token rule
  - protected path rule
  - sensitive file rule
  - user-management rule
- [ ] Emit structured safety metadata into plan, events, result, and `report.md`.

**Tests to add**

- [ ] A safe observational command remains `safe`.
- [ ] A package install becomes `confirm`, not `block`.
- [ ] A core path deletion becomes `block`.
- [ ] A sensitive file overwrite becomes `block`.
- [ ] A recursive permission broadening becomes `confirm` or `block` with explicit reason text.

**Success signal**

- `tests/test_task_service.py` gains a dedicated safety-classification section.
- Risk reasoning is visible in both CLI feedback and persisted audit artifacts.

---

### Task 2: Borrow v4’s explicit workflow-state thinking

**Why**

The current OS_Agent has the right behaviors, but they are not modeled as explicit states. Upstream v4’s state-machine mindset is useful here even if we do not import LangGraph immediately.

**Files**

- Modify: `src/service/models.py`
- Modify: `src/service/orchestrator.py`
- Modify: `src/agent.py`
- Modify: `src/service/audit.py`
- Modify: `tests/test_task_service.py`
- Modify: `tests/test_agent_cli.py`

**What to add**

- [ ] Add an explicit task-state field or state enum for:
  - `planning`
  - `needs_clarification`
  - `awaiting_confirmation`
  - `executing`
  - `analyzing`
  - `recovering`
  - `completed`
  - `failed`
  - `blocked`
- [ ] Standardize when each event is emitted and persisted.
- [ ] Ensure clarification continuation updates state from `needs_clarification` back into planning/execution instead of being treated like a brand-new unrelated task.
- [ ] Include the state transition timeline in `report.md`.

**Tests to add**

- [ ] Clarification path shows explicit state change to `needs_clarification`.
- [ ] User supplement resumes the same logical task.
- [ ] Recovery path records transition into `recovering`.
- [ ] Blocked tasks never advance to execution states.

**Success signal**

- A reviewer can inspect `events.jsonl` or `report.md` and reconstruct the whole workflow.

---

### Task 3: Borrow v4’s config validation and secret discipline

**Why**

This is a high-leverage engineering improvement. It makes the project feel much more serious and reduces runtime surprises during demos.

**Files**

- Modify: `src/config.py`
- Modify: `os_agent.py`
- Modify: `config.yaml`
- Modify: `README.md`
- Add or Modify: `tests/test_project_rename.py` or a new `tests/test_config_validation.py`

**What to add**

- [ ] Add a dedicated config validation command or startup validation pass.
- [ ] Validate required API fields before agent startup.
- [ ] Validate that key local paths exist or can be created safely.
- [ ] Validate that sensitive files use restrictive permissions where appropriate.
- [ ] Keep secrets redacted in printed config and audit output.
- [ ] If Pydantic v2 is available and acceptable, gradually introduce typed validation models for API and file-path settings.

**Tests to add**

- [ ] Missing API key fails with a clear error.
- [ ] Invalid audit path or unwritable path fails early.
- [ ] Printed config output never includes raw secrets.

**Success signal**

- `os_agent.py --check` or equivalent gives a fast pass/fail verdict before runtime.

---

### Task 4: Borrow v4’s scenario harness philosophy

**Why**

The hackathon requires reproducible verification. Upstream v4 uses scenario-driven verification; this is one of the most useful ideas to copy almost directly.

**Files**

- Add: `tests/harness/`
- Add: `tests/harness/scenarios/`
- Add: `tests/harness/run_scenarios.py`
- Add: `tests/harness/scenarios/basic_queries.yaml`
- Add: `tests/harness/scenarios/risk_controls.yaml`
- Add: `tests/harness/scenarios/continuous_tasks.yaml`
- Modify: `README.md`
- Modify: `docs/hackathon/self-test.md`

**What to add**

- [ ] A YAML or JSON driven harness that defines:
  - user input
  - expected plan intent
  - expected state transitions
  - expected risk level
  - expected audit files
  - expected final feedback markers
- [ ] Cover at minimum:
  - disk query
  - file search
  - port/process check
  - user creation
  - user deletion
  - dangerous deletion block
  - clarify-and-resume
  - failure-and-recovery

**Tests to add**

- [ ] Harness runner loads scenarios and reports pass/fail.
- [ ] At least one scenario validates `clarify -> continue`.
- [ ] At least one scenario validates `confirm -> execute`.
- [ ] At least one scenario validates `block`.

**Success signal**

- You can run one command and get a scenario summary suitable for demo prep.

---

### Task 5: Borrow v4’s engineering guardrails selectively

**Why**

This is less flashy than safety and workflow, but it strengthens trust and maintainability.

**Files**

- Modify: `README.md`
- Modify: `.gitignore` if needed
- Add or Modify: lightweight local verification script under `scripts/`
- Optional: `Makefile` targets if you want to move toward upstream structure

**What to add**

- [ ] Add one verification entrypoint that runs:
  - unit tests
  - py_compile
  - scenario harness
- [ ] Add source-level red-line checks where useful:
  - no `shell=True`
  - no unsafe SSH trust bypass if SSH is reintroduced
  - no plaintext secret persistence into audit files
- [ ] Document the safety and verification expectations in README.

**Success signal**

- The repo feels like an engineered product rather than a one-off demo script.

---

## Priority order

Implement in this order:

1. Task 1: hard safety classification
2. Task 2: explicit workflow states
3. Task 4: scenario harness
4. Task 3: config validation
5. Task 5: engineering guardrails

Reason:

- Tasks 1 and 2 improve hackathon scoring most directly.
- Task 4 makes those improvements easy to demonstrate and reproduce.
- Task 3 improves robustness before live demo.
- Task 5 hardens the repo once the feature path is stable.

---

## What NOT to do yet

- Do not rewrite the whole repo into upstream `src/linuxagent/` layout first.
- Do not import LangGraph immediately unless the current orchestrator becomes a blocker.
- Do not spend time on web UI resurrection before the safety/workflow/reporting path is stronger.
- Do not optimize around packaging before the hackathon validation path is excellent.

---

## Recommended execution strategy

If your goal is to maximize visible hackathon value quickly, the next concrete build sequence should be:

1. Strengthen `src/service/risk.py`
2. Make workflow states explicit in `src/service/orchestrator.py`
3. Add scenario harness under `tests/harness/`
4. Add config validation entrypoint in `os_agent.py`
5. Refresh docs and demo scripts only after the above land

