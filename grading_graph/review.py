from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from grading_graph.nodes.aggregator import aggregate_overall
from grading_graph.schemas import (
    CandidateResult,
    FinalResult,
    QuestionResult,
    QuestionVerdict,
    RiskLevel,
    StudentStatus,
    TeacherDecision,
    TranscriptionSpan,
)
from grading_graph.store import atomic_write_json


class ReviewConflict(RuntimeError):
    pass


class ReviewGateError(RuntimeError):
    pass


class ReviewStore:
    """Separate candidate, teacher-decision, and finalized-result persistence."""

    def __init__(self, week_dir: Path | str) -> None:
        self.week_dir = Path(week_dir).resolve()

    @staticmethod
    def student_hash(student_id: str) -> str:
        return hashlib.sha256(str(student_id).encode("utf-8")).hexdigest()

    def _candidate_path(self, student_id: str) -> Path:
        return self.week_dir / "agent_artifacts" / self.student_hash(student_id) / "candidate_result.json"

    def candidate_path(self, student_id: str) -> Path:
        """Return the stable active-candidate path for read-only consumers."""
        return self._candidate_path(student_id)

    def _candidate_version_path(self, student_id: str, run_id: str) -> Path:
        version = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
        return self.week_dir / "agent_artifacts" / self.student_hash(student_id) / "candidate_versions" / f"{version}.json"

    def _decisions_path(self, student_id: str) -> Path:
        return self.week_dir / "review_decisions" / f"{self.student_hash(student_id)}.json"

    def _final_path(self, student_id: str) -> Path:
        return self.week_dir / "results" / f"{self.student_hash(student_id)}.json"

    def save_candidate(self, candidate: CandidateResult) -> Path:
        current = self.load_candidate(candidate.student_id)
        if current is not None:
            decisions = self.load_decisions(candidate.student_id)
            final = self.load_final(candidate.student_id)
            current_revision = max((item.revision for item in decisions), default=1)
            if final is not None and final.finalized and final.revision == current_revision:
                raise ReviewGateError("cannot replace a finalized candidate; reopen the review first")
            if current.run_id != candidate.run_id:
                version_path = self._candidate_version_path(candidate.student_id, current.run_id)
                if not version_path.is_file():
                    atomic_write_json(version_path, current.model_dump(mode="json"))
        return atomic_write_json(self._candidate_path(candidate.student_id), candidate.model_dump(mode="json"))

    def load_candidate(self, student_id: str) -> CandidateResult | None:
        path = self._candidate_path(student_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return CandidateResult.model_validate(value)
        except (OSError, ValueError) as exc:
            raise ReviewGateError(f"candidate result is invalid: {path}") from exc

    def load_decisions(self, student_id: str) -> list[TeacherDecision]:
        path = self._decisions_path(student_id)
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            raw_items = value.get("decisions", []) if isinstance(value, dict) else value
            if not isinstance(raw_items, list):
                raise ValueError("decisions must be a list")
            return [TeacherDecision.model_validate(item) for item in raw_items]
        except (OSError, ValueError) as exc:
            raise ReviewGateError(f"review decisions are invalid: {path}") from exc

    def _save_decisions(self, student_id: str, decisions: list[TeacherDecision]) -> Path:
        return atomic_write_json(
            self._decisions_path(student_id),
            {"student_hash": self.student_hash(student_id), "decisions": [item.model_dump(mode="json") for item in decisions]},
        )

    def load_final(self, student_id: str) -> FinalResult | None:
        path = self._final_path(student_id)
        if not path.is_file():
            return None
        try:
            return FinalResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ReviewGateError(f"final result is invalid: {path}") from exc

    @staticmethod
    def _apply_decision(candidate: CandidateResult, decision: TeacherDecision) -> CandidateResult:
        if decision.action == "reopen":
            return candidate
        if decision.question_id is None:
            raise ReviewGateError("question decision requires question_id")
        current = candidate.question_results.get(decision.question_id)
        if current is None:
            raise ReviewGateError(f"question does not exist: {decision.question_id}")

        updates: dict[str, Any] = {}
        if decision.action == "accept":
            updates.update(needs_verification=False, risk_level=RiskLevel.LOW)
        elif decision.action == "mark_unreadable":
            updates.update(verdict=QuestionVerdict.UNREADABLE, needs_verification=False, risk_level=RiskLevel.LOW)
        elif decision.action == "edit":
            if decision.edited_verdict is None:
                raise ReviewGateError("edit decision requires edited_verdict")
            updates["verdict"] = decision.edited_verdict
            if decision.edited_transcription is not None:
                updates["transcription"] = decision.edited_transcription
            updates.update(needs_verification=False, risk_level=RiskLevel.LOW)
        elif decision.action == "rerun" and decision.rerun_run_id and decision.rerun_run_id == candidate.run_id:
            # A successful targeted rerun is recorded as an audit event.  When
            # the candidate run id matches that event, replay must not put the
            # freshly rerun question back into the risk queue.
            updates.update()
        elif decision.action in {"reject", "rerun"}:
            updates.update(needs_verification=True, risk_level=RiskLevel.HIGH)
        # Re-validate teacher edits instead of using model_copy(update=...),
        # which intentionally bypasses Pydantic validators.  This keeps the
        # evidence gate active when a teacher changes a verdict to partial or
        # incorrect.
        try:
            updated_question = QuestionResult.model_validate(
                {**current.model_dump(mode="json"), **updates}
            )
        except ValueError as exc:
            raise ReviewGateError("teacher edit violates the question evidence gate") from exc
        question_results = dict(candidate.question_results)
        question_results[decision.question_id] = updated_question
        overall, status, unresolved = aggregate_overall(question_results.values())
        return candidate.model_copy(
            update={
                "question_results": question_results,
                "overall": overall,
                "status": status,
                "unresolved_risk_count": unresolved,
            }
        )

    def current_candidate(self, student_id: str, fallback: CandidateResult | None = None) -> CandidateResult:
        candidate = self.load_candidate(student_id) or fallback
        if candidate is None:
            raise ReviewGateError(f"candidate result not found: {student_id}")
        for decision in self.load_decisions(student_id):
            candidate = self._apply_decision(candidate, decision)
        return candidate

    def _current_revision(self, student_id: str) -> int:
        decisions = self.load_decisions(student_id)
        return max((item.revision for item in decisions), default=1)

    def snapshot(self, student_id: str, *, fallback: CandidateResult | None = None) -> dict[str, Any]:
        candidate = self.current_candidate(student_id, fallback)
        decisions = self.load_decisions(student_id)
        final = self.load_final(student_id)
        revision = max((item.revision for item in decisions), default=1)
        active_final = final if final is not None and final.revision == revision and final.finalized else None
        return {
            "candidate": candidate.model_dump(mode="json"),
            "decisions": [item.model_dump(mode="json") for item in decisions],
            "final": active_final.model_dump(mode="json") if active_final else None,
            "revision": revision,
            "status": candidate.status.value,
            "submitReady": bool(active_final and active_final.submit_ready),
        }

    def _check_revision(self, student_id: str, expected_revision: int | None) -> int:
        current = self._current_revision(student_id)
        if expected_revision is not None and expected_revision != current:
            raise ReviewConflict(f"revision conflict: expected {expected_revision}, current {current}")
        final = self.load_final(student_id)
        if final is not None and final.finalized and final.revision == current:
            raise ReviewConflict("review is finalized; reopen it before editing")
        return current

    def record_decision(
        self,
        student_id: str,
        decision: TeacherDecision,
        *,
        expected_revision: int | None = None,
        fallback: CandidateResult | None = None,
    ) -> dict[str, Any]:
        current_revision = self._check_revision(student_id, expected_revision)
        candidate = self.current_candidate(student_id, fallback)
        if decision.revision != current_revision + 1:
            decision = decision.model_copy(update={"revision": current_revision + 1})
        # Validate that this decision can be replayed before persisting it.
        self._apply_decision(candidate, decision)
        decisions = [*self.load_decisions(student_id), decision]
        self._save_decisions(student_id, decisions)
        return self.snapshot(student_id, fallback=fallback)

    def finalize(
        self,
        student_id: str,
        *,
        teacher_id: str,
        expected_revision: int | None = None,
        fallback: CandidateResult | None = None,
    ) -> dict[str, Any]:
        current_revision = self._check_revision(student_id, expected_revision)
        candidate = self.current_candidate(student_id, fallback)
        if candidate.unresolved_risk_count != 0:
            raise ReviewGateError("cannot finalize while unresolved risks remain")
        final_candidate = candidate.model_copy(update={"status": StudentStatus.FINALIZED})
        final = FinalResult(
            candidate=final_candidate,
            decisions=self.load_decisions(student_id),
            finalized=True,
            submit_ready=True,
            finalized_by=teacher_id,
            finalized_at=datetime.now(timezone.utc).isoformat(),
            revision=current_revision,
        )
        atomic_write_json(self._final_path(student_id), final.model_dump(mode="json"))
        return self.snapshot(student_id, fallback=fallback)

    def reopen(
        self,
        student_id: str,
        *,
        teacher_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        current_revision = self._current_revision(student_id)
        final = self.load_final(student_id)
        if final is None or not final.finalized or final.revision != current_revision:
            raise ReviewGateError("no active finalized result to reopen")
        if expected_revision is not None and expected_revision != current_revision:
            raise ReviewConflict(f"revision conflict: expected {expected_revision}, current {current_revision}")
        decision = TeacherDecision(
            action="reopen",
            revision=current_revision + 1,
            teacher_id=teacher_id,
            note="reopened",
        )
        self._save_decisions(student_id, [*self.load_decisions(student_id), decision])
        return self.snapshot(student_id)

    def can_submit(self, student_id: str) -> bool:
        final = self.load_final(student_id)
        return bool(final and final.finalized and final.submit_ready and final.revision == self._current_revision(student_id))

    def _submission_path(self, student_id: str) -> Path:
        return self.week_dir / "submission_receipts" / f"{self.student_hash(student_id)}.json"

    def prepare_submission(self, student_id: str) -> dict[str, Any]:
        """Return one stable idempotency key for the active finalized revision."""
        final = self.load_final(student_id)
        current_revision = self._current_revision(student_id)
        if final is None or not final.finalized or not final.submit_ready or final.revision != current_revision:
            raise ReviewGateError("未 finalized 的结果禁止提交")
        submission_id = hashlib.sha256(
            f"{self.student_hash(student_id)}:{final.candidate.run_id}:{final.revision}".encode("utf-8")
        ).hexdigest()
        path = self._submission_path(student_id)
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReviewGateError("submission receipt is invalid") from exc
            if (
                isinstance(existing, dict)
                and existing.get("submission_id") == submission_id
                and existing.get("revision") == final.revision
            ):
                return {**existing, "already_prepared": True}
        receipt = {
            "schema_version": "1.0",
            "student_hash": self.student_hash(student_id),
            "candidate_run_id": final.candidate.run_id,
            "revision": final.revision,
            "submission_id": submission_id,
            "status": "prepared",
        }
        atomic_write_json(path, receipt)
        return {**receipt, "already_prepared": False}

    def queue(self, candidates: Iterable[tuple[str, CandidateResult]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for student_id, fallback in candidates:
            try:
                snapshot = self.snapshot(student_id, fallback=fallback)
                candidate = snapshot["candidate"]
                rows.append(
                    {
                        "studentId": student_id,
                        "status": snapshot["status"],
                        "overall": candidate.get("overall", "unknown"),
                        "unresolvedRiskCount": int(candidate.get("unresolved_risk_count", 0)),
                        "revision": snapshot["revision"],
                        "submitReady": snapshot["submitReady"],
                    }
                )
            except ReviewGateError as exc:
                rows.append(
                    {
                        "studentId": student_id,
                        "status": "pipeline_failed",
                        "errorType": type(exc).__name__,
                        "revision": 0,
                    }
                )
        return rows
