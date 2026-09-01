from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StudentStatus(str, Enum):
    PENDING = "pending"
    AI_RUNNING = "ai_running"
    CANDIDATE_READY = "candidate_ready"
    REVIEW_REQUIRED = "review_required"
    UNREADABLE = "unreadable"
    REFERENCE_MISMATCH = "reference_mismatch"
    PIPELINE_FAILED = "pipeline_failed"
    IN_REVIEW = "in_review"
    FINALIZED = "finalized"
    SUBMIT_READY = "submit_ready"
    SUBMITTED = "submitted"


class OverallLabel(str, Enum):
    ALL_CORRECT = "all_correct"
    PARTIAL = "partial"
    MANY_ERRORS = "many_errors"
    UNREADABLE = "unreadable"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class QuestionVerdict(str, Enum):
    CORRECT = "correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"
    UNREADABLE = "unreadable"
    MISMATCH = "mismatch"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FileRef(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str | None = None

    @field_validator("path")
    @classmethod
    def reject_embedded_content(cls, value: str) -> str:
        lowered = value.lower()
        if lowered.startswith("data:") or "base64," in lowered:
            raise ValueError("FileRef must point to an artifact path, not embedded bytes")
        return value


class EvidenceRef(StrictModel):
    span_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    bbox: tuple[int, int, int, int]
    artifact_ref: str = Field(min_length=1)
    view: Literal["original", "normalized", "enhanced", "crop"] = "original"

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = value
        if min(value) < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("bbox must be non-negative and have positive width/height")
        return value


class SymbolCandidate(StrictModel):
    symbol: Literal["minus", "blank", "equals", "fraction_bar", "erasure", "unknown"]
    confidence: float = Field(ge=0, le=1)


class TranscriptionSpan(StrictModel):
    span_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    bbox: tuple[int, int, int, int]
    text: str
    symbol_candidates: list[SymbolCandidate] = Field(default_factory=list)
    readability: Literal["clear", "uncertain", "unreadable"]
    confidence: float = Field(ge=0, le=1)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = value
        if min(value) < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("bbox must be non-negative and have positive width/height")
        return value


class PageArtifact(StrictModel):
    page: int = Field(ge=1)
    original: FileRef
    rectified: FileRef | None = None
    normalized: FileRef | None = None
    enhanced: FileRef | None = None
    quality: dict[str, Any] = Field(default_factory=dict)
    page_type: Literal["assignment", "cover", "blank", "wrong_subject", "unknown"] = "unknown"


class AnswerSliceRef(StrictModel):
    question_id: str = Field(min_length=1)
    artifact_ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    character_count: int = Field(ge=0)
    # Keep deterministic compiler metadata beside the answer artifact
    # reference so routing/grading do not need to reparse TeX.
    heading: str = ""
    aliases: list[str] = Field(default_factory=list)
    question_type: str = "unknown"
    problem: str = ""
    reference_answer: str = ""
    rubric_items: list[dict[str, str]] = Field(default_factory=list)
    critical_symbols: list[str] = Field(default_factory=list)
    deterministic_checks: list[str] = Field(default_factory=list)
    source_range: tuple[int, int] | None = None


class AnswerManifest(StrictModel):
    assignment_id: str = Field(min_length=1)
    answer_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str = Field(min_length=1)
    questions: dict[str, AnswerSliceRef] = Field(default_factory=dict)
    reference_status: Literal["ready", "mismatch", "needs_review"] = "ready"

    @model_validator(mode="after")
    def reject_credentials_or_bytes(self) -> "AnswerManifest":
        from grading_graph.store import _validate_no_secret

        try:
            _validate_no_secret(self.model_dump(mode="python"))
        except ValueError:
            raise ValueError("answer manifest contains prohibited secret or bytes") from None
        return self


class QuestionJob(StrictModel):
    question_id: str = Field(min_length=1)
    pages: list[int] = Field(default_factory=list)
    roi_refs: list[EvidenceRef] = Field(default_factory=list)
    answer_slice: AnswerSliceRef | None = None
    question_type: str = "unknown"
    route: Literal["fast", "risk", "unreadable", "mismatch"] = "fast"


class RubricDecision(StrictModel):
    rubric_id: str = Field(min_length=1)
    status: Literal["correct", "partial", "incorrect", "unknown", "unreadable"]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def require_evidence_for_deductions(self) -> "RubricDecision":
        if self.status in {"partial", "incorrect"} and not self.evidence_refs:
            raise ValueError("evidence_refs is required for partial or incorrect rubric decisions")
        return self


class QuestionResult(StrictModel):
    question_id: str = Field(min_length=1)
    verdict: QuestionVerdict
    rubric_decisions: list[RubricDecision] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    transcription: list[TranscriptionSpan] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    needs_verification: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    verifier_result: dict[str, Any] | None = None
    evidence_status: Literal[
        "ready",
        "image_only",
        "incomplete",
        "missing_route",
        "mismatch",
        "provider_error",
    ] = "ready"
    resolution_status: Literal[
        "graded",
        "rescued",
        "needs_rescue",
        "provider_failed",
        "not_applicable",
    ] = "graded"
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_evidence_for_question_deduction(self) -> "QuestionResult":
        if self.verdict in {QuestionVerdict.PARTIAL, QuestionVerdict.INCORRECT}:
            refs = [*self.evidence_refs]
            refs.extend(ref for decision in self.rubric_decisions for ref in decision.evidence_refs)
            if not refs:
                raise ValueError("evidence_refs is required for partial or incorrect question results")
        return self


class Budget(StrictModel):
    max_calls: int = Field(default=0, ge=0)
    max_input_tokens: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    max_image_pixels: int = Field(default=0, ge=0)
    max_cost: float = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    image_pixels: int = Field(default=0, ge=0)
    cost: float = Field(default=0, ge=0)

    def can_consume(self, *, calls: int = 0, input_tokens: int = 0, output_tokens: int = 0) -> bool:
        return (
            self.calls + calls <= self.max_calls
            and self.input_tokens + input_tokens <= self.max_input_tokens
            and self.output_tokens + output_tokens <= self.max_output_tokens
        )


class AuditInfo(StrictModel):
    model: str = "fake"
    provider: str = "fake"
    prompt_version: str = "unknown"
    preprocess_version: str = "unknown"
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_hit: bool = False
    langsmith_enabled: bool = False


class CandidateResult(StrictModel):
    schema_version: str = "1.0"
    graph_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    student_id: str = Field(min_length=1)
    status: StudentStatus
    overall: OverallLabel
    question_results: dict[str, QuestionResult] = Field(default_factory=dict)
    unresolved_risk_count: int = Field(default=0, ge=0)
    candidate_text: str = ""
    errors: list[dict[str, Any]] = Field(default_factory=list)
    legacy_projection: dict[str, Any] = Field(default_factory=dict)
    budget_usage: dict[str, Any] = Field(default_factory=dict)
    audit: AuditInfo | None = None

    @model_validator(mode="after")
    def reject_credentials_or_bytes(self) -> "CandidateResult":
        from grading_graph.store import _validate_no_secret

        try:
            _validate_no_secret(self.model_dump(mode="python"))
        except ValueError:
            raise ValueError("candidate result contains prohibited secret or bytes") from None
        return self

    @model_validator(mode="after")
    def validate_question_keys(self) -> "CandidateResult":
        for question_id, result in self.question_results.items():
            if question_id != result.question_id:
                raise ValueError("question_results keys must match question_id")
        return self


class TeacherDecision(StrictModel):
    question_id: str | None = None
    action: Literal["accept", "edit", "reject", "mark_unreadable", "rerun", "reopen"]
    revision: int = Field(ge=1)
    teacher_id: str = Field(min_length=1)
    edited_transcription: list[TranscriptionSpan] | None = None
    edited_verdict: QuestionVerdict | None = None
    note: str = ""
    rerun_run_id: str | None = None
    created_at: str | None = None


class FinalResult(StrictModel):
    candidate: CandidateResult
    decisions: list[TeacherDecision] = Field(default_factory=list)
    finalized: bool = False
    submit_ready: bool = False
    finalized_by: str | None = None
    finalized_at: str | None = None
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def enforce_finalization_gate(self) -> "FinalResult":
        if self.submit_ready and not self.finalized:
            raise ValueError("submit_ready requires finalized=true")
        if self.finalized:
            if self.candidate.unresolved_risk_count != 0:
                raise ValueError("cannot finalize with unresolved risks")
            if not self.finalized_by or not self.finalized_at:
                raise ValueError("finalized_by and finalized_at are required when finalized")
        return self


class GraphState(StrictModel):
    schema_version: str = "1.0"
    graph_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    student_id: str = Field(min_length=1)
    answer_manifest: AnswerManifest | None = None
    pages: list[PageArtifact] = Field(default_factory=list)
    page_observations: list[dict[str, Any]] = Field(default_factory=list)
    local_layout: dict[str, Any] = Field(default_factory=dict)
    layout_audit: list[dict[str, Any]] = Field(default_factory=list)
    question_jobs: dict[str, QuestionJob] = Field(default_factory=dict)
    question_results: dict[str, QuestionResult] = Field(default_factory=dict)
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    retries: dict[str, int] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    audit: AuditInfo | None = None
    final_projection: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_credentials_or_bytes(self) -> "GraphState":
        from grading_graph.store import _validate_no_secret

        try:
            _validate_no_secret(self.model_dump(mode="python"))
        except ValueError:
            raise ValueError("graph state contains prohibited secret or bytes") from None
        return self

    @field_validator("student_id")
    @classmethod
    def no_embedded_secret(cls, value: str) -> str:
        if re.search(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|dashscope-[A-Za-z0-9]{24,})", value):
            raise ValueError("student_id contains a secret-like value")
        return value
