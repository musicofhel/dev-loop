"""Ambiguity detection for issue triage (#32).

Lightweight heuristic analysis of issue title + description to detect
underspecified issues that would waste agent budget.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

VAGUE_VERBS: frozenset[str] = frozenset({
    "improve", "clean", "cleanup", "enhance", "optimize",
    "refactor", "make better", "update", "address",
    "handle", "deal with", "look at", "look into", "investigate",
    "review", "check", "tweak", "adjust", "polish", "tidy",
    "streamline", "simplify", "rework",
})

_VAGUE_VERB_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in sorted(VAGUE_VERBS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_SPECIFICITY_PATTERNS = [
    re.compile(r"\b\w+\.\w{1,4}\b"),                              # file refs: foo.py
    re.compile(r"\b(function|method|class|def|fn)\s+\w+", re.I),  # named code elements
    re.compile(r"`[^`]+`"),                                        # inline code
    re.compile(r"(given|when|then|should|expect|assert)", re.I),   # acceptance criteria
    re.compile(r"(error|exception|crash|stack\s*trace|traceback)", re.I),  # symptoms
    re.compile(r"(line\s*\d+|L\d+)", re.I),                       # line numbers
    re.compile(r"https?://\S+"),                                   # URLs
]

SHORT_DESCRIPTION_THRESHOLD = 20


@dataclass
class AmbiguitySignal:
    signal_type: str
    detail: str
    weight: float = 1.0


@dataclass
class AmbiguityResult:
    is_ambiguous: bool
    score: float
    signals: list[AmbiguitySignal] = field(default_factory=list)
    title: str = ""
    description: str = ""

    @property
    def summary(self) -> str:
        if not self.signals:
            return "Issue appears sufficiently specified."
        parts = [s.signal_type for s in self.signals]
        return f"Ambiguity signals: {', '.join(parts)} (score={self.score:.2f})"


def detect_ambiguity(title: str, description: str | None = None, threshold: float = 0.6) -> AmbiguityResult:
    """Analyze issue for ambiguity signals. Score >= threshold = ambiguous."""
    desc = description or ""
    combined = f"{title} {desc}".strip()

    signals: list[AmbiguitySignal] = []
    signals.extend(_check_vague_verbs(combined))

    short = _check_description_length(desc)
    if short:
        signals.append(short)

    spec = _check_specificity(combined)
    if spec:
        signals.append(spec)

    acc = _check_acceptance_criteria(combined)
    if acc:
        signals.append(acc)

    score = _compute_score(signals)

    return AmbiguityResult(
        is_ambiguous=score >= threshold,
        score=score,
        signals=signals,
        title=title,
        description=desc,
    )


def _check_vague_verbs(text: str) -> list[AmbiguitySignal]:
    matches = _VAGUE_VERB_PATTERN.findall(text)
    if matches:
        unique = sorted(set(m.lower() for m in matches))
        return [AmbiguitySignal(
            signal_type="vague_verb",
            detail=f"Vague action verb(s): {', '.join(unique)}",
            weight=0.3,
        )]
    return []


def _check_description_length(description: str | None) -> AmbiguitySignal | None:
    if not description or not description.strip():
        return AmbiguitySignal(
            signal_type="short_description",
            detail="No description provided",
            weight=0.25,
        )
    words = description.split()
    if len(words) < SHORT_DESCRIPTION_THRESHOLD:
        return AmbiguitySignal(
            signal_type="short_description",
            detail=f"Description is only {len(words)} words (threshold: {SHORT_DESCRIPTION_THRESHOLD})",
            weight=0.25,
        )
    return None


def _check_specificity(text: str) -> AmbiguitySignal | None:
    for pattern in _SPECIFICITY_PATTERNS:
        if pattern.search(text):
            return None
    return AmbiguitySignal(
        signal_type="no_specifics",
        detail="No file paths, code references, error messages, or URLs found",
        weight=0.25,
    )


def _check_acceptance_criteria(text: str) -> AmbiguitySignal | None:
    criteria_pattern = re.compile(
        r"\b(given|when|then|should|expect|assert|must|returns?|throws?)\b", re.I,
    )
    if criteria_pattern.search(text):
        return None
    return AmbiguitySignal(
        signal_type="no_acceptance_criteria",
        detail="No acceptance criteria language (should/expect/given/when/then)",
        weight=0.2,
    )


def _compute_score(signals: list[AmbiguitySignal]) -> float:
    if not signals:
        return 0.0
    total = sum(s.weight for s in signals)
    return min(total, 1.0)


def defer_ambiguous_issue(issue_id: str, result: AmbiguityResult) -> bool:
    """Defer an ambiguous issue: add needs-clarification label, set deferred status, add comment."""
    try:
        comment = f"Auto-deferred: {result.summary}\n\nSignals:\n"
        for s in result.signals:
            comment += f"- {s.signal_type}: {s.detail}\n"

        # Add label
        subprocess.run(
            ["br", "label", issue_id, "needs-clarification"],
            capture_output=True, text=True, timeout=30,
        )
        # Add comment
        subprocess.run(
            ["br", "comments", "add", issue_id, comment],
            capture_output=True, text=True, timeout=30,
        )
        # Set status
        result_proc = subprocess.run(
            ["br", "update", issue_id, "--status", "deferred"],
            capture_output=True, text=True, timeout=30,
        )
        return result_proc.returncode == 0
    except Exception:
        logger.exception("Failed to defer issue %s", issue_id)
        return False
