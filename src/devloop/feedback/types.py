"""Pydantic models for the feedback loop layer."""

from __future__ import annotations

from pydantic import BaseModel, Field

from devloop.gates.types import GateSuiteResult


class RetryPrompt(BaseModel):
    """A constructed retry prompt with failure context for the agent."""

    prompt_text: str = Field(
        description="The full retry prompt including all failure details.",
    )
    failure_count: int = Field(
        default=0,
        ge=0,
        description="Number of gate failures encoded in this prompt.",
    )


class RetryResult(BaseModel):
    """Result of a retry attempt (agent re-spawn + gate re-run)."""

    attempt: int = Field(
        description="Which retry attempt this was (1-indexed).",
    )
    max_retries: int = Field(
        description="Maximum retries allowed for this run.",
    )
    success: bool = Field(
        description="True if all gates passed after this retry.",
    )
    gate_results: GateSuiteResult | None = Field(
        default=None,
        description="Gate suite results from the retry run (None if agent failed to spawn).",
    )
    escalated: bool = Field(
        default=False,
        description="True if retries were exhausted and the issue was escalated to a human.",
    )
    agent_exit_code: int = Field(
        default=-1,
        description="Exit code from the agent process during this retry.",
    )
    error: str | None = Field(
        default=None,
        description="Error message if the retry failed at infrastructure level.",
    )


class EscalationResult(BaseModel):
    """Result of escalating a failed issue to a human."""

    issue_id: str
    success: bool
    status_updated: bool = False
    comment_added: bool = False
    attempts: int = 0
    message: str = ""


class TB1Result(BaseModel):
    """Result of a full TB-1 pipeline run."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(
        description="Last phase completed (or failed at).",
    )
    worktree_path: str | None = None
    persona: str | None = None
    agent_exit_code: int | None = None
    gate_results: GateSuiteResult | None = None
    retries_used: int = 0
    max_retries: int = 0
    escalated: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    pr_url: str | None = None


class SecurityFinding(BaseModel):
    """A security finding from Gate 3 with CWE classification."""

    cwe: str | None = None
    severity: str = "critical"
    message: str = ""
    file: str | None = None
    line: int | None = None
    rule: str | None = None
    fixed: bool = False


class TB3Result(BaseModel):
    """Result of a full TB-3 pipeline run (Security-Gate-to-Fix)."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(
        description="Last phase completed (or failed at).",
    )
    worktree_path: str | None = None
    persona: str | None = None
    retries_used: int = 0
    max_retries: int = 0
    escalated: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    pr_url: str | None = None
    # TB-3 specific fields
    trace_id: str | None = Field(
        default=None,
        description="Root OTel trace ID for trace verification.",
    )
    attempt_span_ids: list[str] = Field(
        default_factory=list,
        description="Span IDs per attempt for linked trace verification.",
    )
    security_findings: list[SecurityFinding] = Field(
        default_factory=list,
        description="Security findings detected by Gate 3 on initial scan.",
    )
    vulnerability_fixed: bool = Field(
        default=False,
        description="True if the security vulnerability was fixed after retry.",
    )
    cwe_ids: list[str] = Field(
        default_factory=list,
        description="CWE IDs detected (e.g. ['CWE-89'] for SQL injection).",
    )
    vuln_seeded: bool = Field(
        default=False,
        description="Whether vulnerable code was pre-seeded (forced mode).",
    )
    retry_history: list[RetryAttempt] = Field(
        default_factory=list,
        description="Per-attempt summary with gate results and span IDs.",
    )


class UsageBreakdown(BaseModel):
    """Per-attempt usage stats for TB-4 turn/token tracking."""

    attempt: int
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cumulative_turns: int = 0
    context_pct_at_exit: float = Field(
        default=0.0,
        description="Context window percentage when the agent exited.",
    )
    context_restart: bool = Field(
        default=False,
        description="True if this attempt was a context restart (fresh session with handoff).",
    )


class TB4Result(BaseModel):
    """Result of a full TB-4 pipeline run (Runaway-to-Stop)."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(
        description="Last phase completed (or failed at).",
    )
    worktree_path: str | None = None
    persona: str | None = None
    retries_used: int = 0
    max_retries: int = 0
    escalated: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    # TB-4 specific fields
    trace_id: str | None = Field(
        default=None,
        description="Root OTel trace ID for trace verification.",
    )
    attempt_span_ids: list[str] = Field(
        default_factory=list,
        description="Span IDs per attempt for linked trace verification.",
    )
    turns_used_total: int = Field(
        default=0,
        description="Total agentic turns consumed across all attempts.",
    )
    max_turns_total: int = Field(
        default=0,
        description="Total turn budget from persona config (or override).",
    )
    usage_breakdown: list[UsageBreakdown] = Field(
        default_factory=list,
        description="Per-attempt turn/token breakdown.",
    )
    context_restarts: int = Field(
        default=0,
        description="Number of context-limit restarts (fresh sessions with handoff notes).",
    )


class RetryAttempt(BaseModel):
    """Summary of a single retry attempt for TB-2 tracking."""

    attempt: int
    agent_exit_code: int = -1
    gates_passed: bool = False
    first_failure: str | None = None
    span_id: str | None = None


class TB5Result(BaseModel):
    """Result of a full TB-5 pipeline run (Cross-Repo Cascade)."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(
        description="Last phase completed (or failed at).",
    )
    error: str | None = None
    duration_seconds: float = 0.0
    # TB-5 specific fields
    target_repo_path: str = Field(
        default="",
        description="Absolute path to the target repository.",
    )
    target_issue_id: str | None = Field(
        default=None,
        description="Beads issue ID created in the target repo for the cascade.",
    )
    changed_files: list[str] = Field(
        default_factory=list,
        description="Files changed on the source branch (git diff).",
    )
    matched_watches: list[str] = Field(
        default_factory=list,
        description="Watch glob patterns that matched changed files.",
    )
    dependency_type: str | None = Field(
        default=None,
        description="Dependency type from config (e.g. 'api-contract').",
    )
    cascade_skipped: bool = Field(
        default=False,
        description="True if no watch patterns matched (cascade not needed).",
    )
    tb1_result: dict | None = Field(
        default=None,
        description="Full TB-1 result from running the cascade pipeline on the target repo.",
    )
    source_comment_added: bool = Field(
        default=False,
        description="True if an outcome comment was added to the source issue.",
    )


class SessionEvent(BaseModel):
    """A single event parsed from the agent's NDJSON output."""

    line_number: int = Field(description="1-indexed line number in the NDJSON output.")
    type: str = Field(description="Event type (result, assistant, tool_use, etc.).")
    data: dict = Field(default_factory=dict, description="Full parsed JSON object.")


class TB6Result(BaseModel):
    """Result of a full TB-6 pipeline run (Session Replay Debug)."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(description="Last phase completed (or failed at).")
    error: str | None = None
    duration_seconds: float = 0.0
    worktree_path: str | None = None
    persona: str | None = None
    retries_used: int = 0
    max_retries: int = 0
    escalated: bool = False
    # TB-6 specific fields
    trace_id: str | None = Field(
        default=None,
        description="Root OTel trace ID.",
    )
    attempt_span_ids: list[str] = Field(
        default_factory=list,
        description="Span IDs per attempt for linked trace verification.",
    )
    session_id: str | None = Field(
        default=None,
        description="Session ID for the captured agent run.",
    )
    session_path: str | None = Field(
        default=None,
        description="Filesystem path to the saved session NDJSON file.",
    )
    session_event_count: int = Field(
        default=0,
        description="Total number of NDJSON events in the session.",
    )
    session_event_types: dict[str, int] = Field(
        default_factory=dict,
        description="Count of events by type (e.g. {'tool_use': 3, 'result': 1}).",
    )
    gate_failure: str | None = Field(
        default=None,
        description="Name of the first gate that failed (if any).",
    )
    suggested_fix: str | None = Field(
        default=None,
        description="Suggested CLAUDE.md rule based on gate failure analysis.",
    )
    force_gate_fail_used: bool = Field(
        default=False,
        description="Whether forced gate failure mode was active.",
    )


class TB2Result(BaseModel):
    """Result of a full TB-2 pipeline run."""

    issue_id: str
    repo_path: str
    success: bool
    phase: str = Field(
        description="Last phase completed (or failed at).",
    )
    worktree_path: str | None = None
    persona: str | None = None
    retries_used: int = 0
    max_retries: int = 0
    escalated: bool = False
    error: str | None = None
    duration_seconds: float = 0.0
    pr_url: str | None = None
    # TB-2 specific fields
    trace_id: str | None = Field(
        default=None,
        description="Root OTel trace ID for trace verification.",
    )
    attempt_span_ids: list[str] = Field(
        default_factory=list,
        description="Span IDs per attempt for linked trace verification.",
    )
    blocked_verified: bool = Field(
        default=False,
        description="True if issue status was verified as 'blocked' after escalation.",
    )
    force_gate_fail_used: bool = Field(
        default=False,
        description="Whether forced gate failure mode was active.",
    )
    retry_history: list[RetryAttempt] = Field(
        default_factory=list,
        description="Per-attempt summary with gate results and span IDs.",
    )


class FindingComparison(BaseModel):
    """Side-by-side comparison of a single finding from DSPy vs CLI paths."""

    dspy_message: str = ""
    cli_message: str = ""
    severity_match: bool = False
    message_similarity: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Jaccard similarity between finding messages.",
    )


class TB7Result(BaseModel):
    """Result of a TB-7 pipeline run (LLMOps A/B Comparison)."""

    repo_path: str
    success: bool
    phase: str = Field(description="Last phase completed (or failed at).")
    error: str | None = None
    duration_seconds: float = 0.0
    # TB-7 specific fields
    artifact_version: str | None = Field(
        default=None,
        description="Version string of the loaded optimization artifact.",
    )
    artifact_metric_score: float | None = Field(
        default=None,
        description="Metric score recorded in the artifact metadata.",
    )
    training_example_count: int = Field(
        default=0,
        description="Number of training examples in code_review.jsonl.",
    )
    dspy_finding_count: int = Field(
        default=0,
        description="Number of findings from the DSPy (optimized) path.",
    )
    cli_finding_count: int = Field(
        default=0,
        description="Number of findings from the CLI (baseline) path.",
    )
    dspy_findings: list[dict] = Field(
        default_factory=list,
        description="Raw findings from DSPy path.",
    )
    cli_findings: list[dict] = Field(
        default_factory=list,
        description="Raw findings from CLI path.",
    )
    dspy_latency_seconds: float = Field(
        default=0.0,
        description="Wall-clock time for the DSPy path.",
    )
    cli_latency_seconds: float = Field(
        default=0.0,
        description="Wall-clock time for the CLI path.",
    )
    latency_ratio: float = Field(
        default=0.0,
        description="DSPy latency / CLI latency (< 1.0 means DSPy is faster).",
    )
    finding_count_delta: int = Field(
        default=0,
        description="DSPy count - CLI count (positive = DSPy found more).",
    )
    message_overlap_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Average Jaccard similarity of matched finding messages.",
    )
    severity_agreement_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of matched findings where severity agrees.",
    )
