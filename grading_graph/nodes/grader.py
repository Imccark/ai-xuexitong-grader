from __future__ import annotations

import json
import ast
import math
import re
import time
from typing import Any, Protocol, Sequence

from grading_graph.schemas import EvidenceRef, QuestionJob, QuestionResult, QuestionVerdict, RiskLevel, TranscriptionSpan


class GradingProvider(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any], image_ref: str | None = None) -> dict[str, Any]: ...


class GradingProviderError(ValueError):
    """A provider response cannot be safely converted to a grading result."""


GRADER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"enum": [item.value for item in QuestionVerdict]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_verification": {"type": "boolean"},
        "risk_level": {"enum": [item.value for item in RiskLevel]},
        "evidence_refs": {"type": "array"},
        "rubric_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "rubric_id": {"type": "string"},
                    "status": {"enum": ["correct", "partial", "incorrect", "unknown", "unreadable"]},
                    "reason": {"type": "string"},
                    "evidence_refs": {"type": "array"},
                },
                "required": ["rubric_id", "status", "reason", "evidence_refs"],
            },
        },
    },
    "required": ["verdict", "confidence", "needs_verification", "risk_level", "evidence_refs", "rubric_decisions"],
}


def _model_dump(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _safe_formula_value(expression: str, variables: dict[str, float]) -> float | None:
    """Evaluate a tiny arithmetic grammar for equivalence spot checks."""

    normalized = str(expression).strip().replace("^", "**")
    normalized = re.sub(r"(?<=\d)(?=[A-Za-z_(])", "*", normalized)
    normalized = re.sub(r"(?<=[A-Za-z_)])(?=\()", "*", normalized)
    if not normalized or re.search(r"[^A-Za-z0-9_()+\-*/.\s]", normalized):
        return None
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in variables:
            return float(variables[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if abs(right) > 8:
                raise ValueError
            return left**right
        raise ValueError

    try:
        value = visit(tree.body)
    except (ArithmeticError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _plain_formulas_equivalent(left: str, right: str) -> bool | None:
    samples = (
        {"a": 5.0, "b": 1.0, "k": 2.0, "t": 3.0},
        {"a": -1.0, "b": 4.0, "k": -2.0, "t": 1.0},
        {"a": 7.0, "b": -3.0, "k": 4.0, "t": -1.0},
    )
    compared = 0
    for variables in samples:
        left_value = _safe_formula_value(left, variables)
        right_value = _safe_formula_value(right, variables)
        if left_value is None or right_value is None:
            continue
        compared += 1
        if not math.isclose(left_value, right_value, rel_tol=1e-9, abs_tol=1e-9):
            return False
    return True if compared >= 2 else None


def _plainify_latex_formula(value: str) -> str:
    """Convert the tiny LaTeX subset used in grader explanations."""

    text = str(value).replace("$", "").replace(r"\cdot", "*")
    fraction = re.compile(r"\\frac\{([^{}]+)\}\{([^{}]+)\}")
    for _ in range(4):
        updated = fraction.sub(r"((\1)/(\2))", text)
        if updated == text:
            break
        text = updated
    return text.replace("{", "(").replace("}", ")")


def _student_reference_formula_conflict(reason: str) -> bool:
    """Spot-check same-variable formulas explicitly contrasted in a reason."""

    if "参考答案" not in reason:
        return False
    student_part, reference_part = reason.split("参考答案", 1)
    for variable in ("x_1", "x_2", "x_3", "x_4"):
        pattern = re.compile(rf"\${re.escape(variable)}\s*=\s*([^$]+)\$")
        student_matches = pattern.findall(student_part)
        reference_matches = pattern.findall(reference_part)
        if not student_matches or not reference_matches:
            continue
        student_formula = _plainify_latex_formula(student_matches[-1]).strip()
        reference_formula = _plainify_latex_formula(reference_matches[0]).strip()
        if _plain_formulas_equivalent(student_formula, reference_formula) is False:
            return True
    return False


class QuestionGrader:
    def __init__(
        self,
        provider: GradingProvider,
        *,
        max_retries: int = 1,
        missing_rubric_retries: int | None = None,
        transient_status_codes: tuple[int, ...] = (408, 429, 500, 502, 503, 504),
        backoff_base: float = 0.05,
        strict_evidence_gate: bool = True,
    ) -> None:
        self.provider = provider
        self.max_retries = max(0, min(max_retries, 2))
        self.missing_rubric_retries = max(
            0,
            min(self.max_retries if missing_rubric_retries is None else missing_rubric_retries, 2),
        )
        self.transient_status_codes = frozenset(int(value) for value in transient_status_codes)
        self.backoff_base = max(0.0, backoff_base)
        self.strict_evidence_gate = bool(strict_evidence_gate)

    def grade(
        self,
        job: QuestionJob | dict[str, Any],
        transcription: Sequence[TranscriptionSpan | dict[str, Any]],
        *,
        answer_text: str = "",
        image_ref: str | None = None,
        _missing_rubric_ids: tuple[str, ...] = (),
    ) -> QuestionResult:
        question_job = QuestionJob.model_validate(job)
        spans = [TranscriptionSpan.model_validate(item) for item in transcription]
        answer_ref = question_job.answer_slice.artifact_ref if question_job.answer_slice else "unavailable"
        answer_hash = question_job.answer_slice.sha256 if question_job.answer_slice else "unavailable"
        rubric_requirements = [
            {
                "rubric_id": str(item.get("rubric_id") or item.get("id") or f"r{index + 1}"),
                "requirement": str(item.get("requirement") or item.get("description") or "")[:1800],
            }
            for index, item in enumerate(
                question_job.answer_slice.rubric_items if question_job.answer_slice else []
            )
            if isinstance(item, dict)
        ]
        coverage_repair = (
            "\n结构修复：上次响应缺少以下 rubric_id，必须全部逐项返回，不能省略："
            + json.dumps(list(_missing_rubric_ids), ensure_ascii=False)
            if _missing_rubric_ids
            else ""
        )
        prompt = (
            "你是逐题批改器。只依据学生转写、题目参考答案切片和当前题目 ROI 做判断，不能凭空补全书写。"
            "先按 rubric_items 逐项核对，再汇总 verdict：correct=所有关键要求均满足且无实质矛盾；"
            "partial=至少一个关键步骤/结论正确，但另有错误、遗漏或证明不完整；"
            "incorrect=没有任何可评分的正确实质，或核心结论完全错误；"
            "unreadable=没有足够清晰的目标题作答证据。不要把‘有正确步骤但最终有一处错误’判成 incorrect，"
            "也不要把‘只写了题号/邻题内容’当作 partial。对分类讨论、矩阵和含负号表达式逐个复核。"
            "若 rubric 要求多个分支/小问，必须确认每个分支都有当前题的证据；证据被截断或缺失时只能判 partial。"
            "对单个 rubric 也遵守部分得分：若该分支的最终分类/结论正确，但推导、公式或通解不完整，"
            "该 rubric 必须标为 partial，不能标为 incorrect；incorrect 仅用于该评分点没有任何正确实质或核心结论错误。"
            "除非题目明确限定方法，否则行列式、秩、行变换等任一数学有效的替代方法都可得满分；"
            "不得仅因学生方法或中间矩阵与参考答案不同而扣分。若正确设置与最终结论已经充分建立答案，"
            "不得仅因省略部分机械化代数步骤而扣分。等价的参数命名、自由变量命名、线性表示及上下文清楚的简写"
            "允许判 correct；应优先用代入、展开或等价变换复核，不能把表达形式不同当成错误。"
            "若同时提供多页且多个页面都有裸(1)/(2)，必须先比较可见的初始矩阵、变量和题面特征与当前参考切片，"
            "选择语义匹配页；不得因某页先出现同名小问就忽略其他页。"
            "但不能用数学常识补写未出现的分支。目标题号或目标题区域清楚可见却没有实质作答时判 incorrect；"
            "只有图像质量使是否作答都无法判断时才判 unreadable。"
            "每个 partial/incorrect rubric 决定都必须携带能定位到学生作答的 evidence_refs；"
            "必须为 rubric_requirements 中的每个 rubric_id 分别输出一条 rubric_decisions，不能合并或省略；"
            "rubric_decisions.status 只能是 correct/partial/incorrect/unknown/unreadable。"
            "若 reason 已经明确断言该评分点满足或不满足，就禁止输出 unknown；unknown 仅用于原图确实无法判断。"
            "整题 verdict=correct 时，每个评分点必须为 correct；若任一评分点为 partial/incorrect/unknown/unreadable，"
            "整题不得输出 correct。"
            "reason 必须简短（每项不超过120字），只写证据和结论；不得输出反复自问自答、猜测过程或长篇思维链。"
            "若证据不足，改为 unreadable 并 needs_verification=true。输出严格 JSON，不输出额外解释。\n"
            f"question_id={question_job.question_id}\n"
            f"answer_slice={answer_ref}\n"
            f"answer_hash={answer_hash}\n"
            f"rubric_requirements={json.dumps(rubric_requirements, ensure_ascii=False)}\n"
            f"allowed_image_evidence={json.dumps([ref.model_dump(mode='json') for ref in question_job.roi_refs], ensure_ascii=False)}\n"
            f"answer_text={answer_text}\n"
            f"route={question_job.route}\n"
            f"transcription={json.dumps([_model_dump(span) for span in spans], ensure_ascii=False)}"
            f"{coverage_repair}"
        )
        payload: Any = None
        for attempt in range(self.max_retries + 1):
            try:
                payload = self.provider.complete_json(prompt, GRADER_SCHEMA, image_ref=image_ref)
                break
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                error_text = str(exc)
                retryable = (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or (isinstance(status_code, int) and status_code in self.transient_status_codes)
                    or any(str(code) in error_text for code in self.transient_status_codes)
                )
                if not retryable or attempt >= self.max_retries:
                    raise GradingProviderError(
                        f"grading provider failed for {question_job.question_id} ({type(exc).__name__})"
                    ) from None
                time.sleep(self.backoff_base * (2**attempt))
        if not isinstance(payload, dict):
            raise GradingProviderError("grader response must be an object")

        raw_verdict = str(payload.get("verdict", "unreadable"))
        evidence_refs = self._valid_evidence_refs(
            payload.get("evidence_refs") or payload.get("corrected_evidence_refs"),
            question_job=question_job,
            spans=spans,
        )
        # Qwen occasionally returns a valid deduction but omits the explicit
        # span reference even though the transcription is clear.  In the
        # graph's non-strict production mode, bind that deduction to the
        # smallest available clear span/ROI instead of collapsing it into an
        # ``unreadable`` result.  Strict/unit-test mode still rejects omitted
        # evidence, preserving the safety gate for untrusted integrations.
        if (
            not evidence_refs
            and not self.strict_evidence_gate
            and raw_verdict in {QuestionVerdict.PARTIAL.value, QuestionVerdict.INCORRECT.value}
        ):
            evidence_refs = self._valid_evidence_refs(
                [span.span_id for span in spans if span.readability == "clear"],
                question_job=question_job,
                spans=spans,
            )
            # A rescue locator can produce a tightly cropped, image-only ROI
            # with no OCR span.  Bind a model deduction to that explicit crop
            # rather than converting a clearly blank/wrong answer to
            # ``unreadable`` merely because transcription was intentionally
            # discarded as incompatible neighboring evidence.
            if not evidence_refs and question_job.roi_refs:
                evidence_refs = [question_job.roi_refs[0].model_dump(mode="json")]
        raw_rubric_decisions = payload.get("rubric_decisions")
        # Qwen frequently calls the same per-rubric array ``rubric_items``
        # (and older prompts used ``rubric_analysis``).  Normalize those
        # aliases before applying the evidence gate so a clear deduction is
        # not incorrectly collapsed into ``unreadable``.
        if not isinstance(raw_rubric_decisions, list) or not raw_rubric_decisions:
            for alias in ("rubric_items", "rubric_analysis", "rubric_assessment"):
                candidate = payload.get(alias)
                if isinstance(candidate, list):
                    raw_rubric_decisions = candidate
                    break
        rubric_decisions = self._valid_rubric_decisions(raw_rubric_decisions, question_job=question_job, spans=spans)
        expected_rubric_ids = {
            str(item.get("rubric_id") or item.get("id") or "")
            for item in rubric_requirements
            if str(item.get("rubric_id") or item.get("id") or "")
        }
        returned_rubric_ids = {
            str(item.get("rubric_id") or "") for item in rubric_decisions
        }
        missing_rubric_ids = tuple(sorted(expected_rubric_ids - returned_rubric_ids))
        if missing_rubric_ids and len(expected_rubric_ids) >= 2 and self.missing_rubric_retries > 0:
            return QuestionGrader(
                self.provider,
                max_retries=self.max_retries,
                missing_rubric_retries=self.missing_rubric_retries - 1,
                transient_status_codes=tuple(self.transient_status_codes),
                backoff_base=self.backoff_base,
                strict_evidence_gate=self.strict_evidence_gate,
            ).grade(
                question_job,
                spans,
                answer_text=answer_text,
                image_ref=image_ref,
                _missing_rubric_ids=missing_rubric_ids,
            )
        # Qwen can label a rubric ``incorrect`` while its own explanation
        # explicitly says the student reached a correct conclusion and only
        # criticizes an incomplete/incorrect derivation.  That contradicts
        # this project's partial-credit contract. Repair only this explicit
        # linguistic contradiction; never infer mathematical correctness from
        # an otherwise negative reason.
        requirement_by_id = {
            str(item.get("rubric_id") or item.get("id") or f"r{index + 1}"): str(
                item.get("requirement") or item.get("description") or ""
            )
            for index, item in enumerate(
                question_job.answer_slice.rubric_items if question_job.answer_slice else []
            )
            if isinstance(item, dict)
        }
        conclusion_tokens = ("无穷多解", "唯一解", "无解", "线性相关", "线性无关", "只有零解", "非零解")
        for decision in rubric_decisions:
            requirement = requirement_by_id.get(str(decision.get("rubric_id", "")), "")
            reason = str(decision.get("reason", ""))
            # If the model itself transcribes two formulas as different, its
            # subsequent claim that they are equivalent or both satisfy the
            # equations must survive a safe arithmetic spot check.  This is a
            # narrow self-consistency guard; it never parses image text as
            # executable code and only downgrades full credit to partial.
            if decision.get("status") == "correct":
                if re.search(
                    r"(?:似乎有误|等等|重新核对|大概率|看不太清|可能是误识别|如果学生写的是|不确定)",
                    reason,
                ):
                    decision["status"] = "partial"
                    continue
                if _student_reference_formula_conflict(reason):
                    decision["status"] = "partial"
                    continue
                formula_pair = re.search(
                    r"学生.{0,50}?写为\s*(?P<student>.*?)，而参考答案为\s*"
                    r"(?P<reference>.*?)(?:，|。)",
                    reason,
                    flags=re.DOTALL,
                )
                student_formula = (
                    re.split(r"[（(]即", formula_pair.group("student"), maxsplit=1)[0].strip()
                    if formula_pair
                    else ""
                )
                reference_formula = formula_pair.group("reference").strip() if formula_pair else ""
                if formula_pair and _plain_formulas_equivalent(student_formula, reference_formula) is False:
                    decision["status"] = "partial"
                    continue
                # Some responses state a concrete equality chain as their
                # proof of equivalence (``x_1 = student = reference``).  Check
                # that chain numerically instead of trusting the prose around
                # it; this caught swapped x1/x2 formulas that looked plausible.
                plain_reason = _plainify_latex_formula(reason)
                equality_chain = re.search(
                    r"x_?\(?\d+\)?\s*=\s*(?P<student>[^=，。]+?)\s*=\s*(?P<reference>[^，。）]+)",
                    plain_reason,
                    flags=re.IGNORECASE,
                )
                if equality_chain and _plain_formulas_equivalent(
                    equality_chain.group("student").strip(),
                    equality_chain.group("reference").strip(),
                ) is False:
                    decision["status"] = "partial"
                    continue
                equivalence_claim = re.search(
                    r"x_?\(?\d+\)?\s*=\s*(?P<student>.+?)\s*即\s*(?P<reference>.+?)(?:[，。）]|$)",
                    plain_reason,
                    flags=re.IGNORECASE,
                )
                if equivalence_claim and _plain_formulas_equivalent(
                    equivalence_claim.group("student").strip(),
                    equivalence_claim.group("reference").strip(),
                ) is False:
                    decision["status"] = "partial"
                    continue
                explicit_error = re.search(
                    r"(?:学生|作答).{0,180}(?:出现|存在|写成|写为).{0,100}"
                    r"(?:错误|漏写|少写|不一致).{0,180}(?:正确应为|不一致|判为\s*partial|部分正确)",
                    reason,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                if explicit_error:
                    decision["status"] = "partial"
                    continue
            # Do not deduct for omitted mechanical algebra when the rubric
            # asks only for the mathematical result and the model's own
            # explanation confirms that the setup and final result are
            # correct.  Any stated wrong sign, formula, conclusion or missing
            # required proof keeps the partial verdict.
            if (
                decision.get("status") == "partial"
                and not re.search(r"(?:过程|步骤|推导|证明|行变换)", requirement)
                and re.search(
                    r"(?:正确.{0,30}(?:最终|结论|结果|表示式)|(?:最终|结论|结果|表示式).{0,80}(?:正确|一致|等价))",
                    reason,
                )
                and re.search(
                    r"(?:但是|但|仅因|只是).{0,120}(?:未展示|未写出|省略|直接给出).{0,100}(?:过程|步骤|计算|推导)",
                    reason,
                    flags=re.DOTALL,
                )
                and not re.search(
                    r"(?:符号|公式|结论|结果|答案).{0,50}(?:错误|不正确|不一致|遗漏)",
                    reason,
                )
            ):
                decision["status"] = "correct"
                continue
            # In a question that explicitly asks for beta's linear
            # representation, writing the fully correct combination on the
            # final line is a context-complete answer even if the writer omits
            # the redundant ``beta =`` prefix.  Do not treat that shorthand as
            # a mathematical omission after the coefficients were verified.
            if (
                decision.get("status") == "partial"
                and re.search(r"(?:系数|解).{0,40}(?:\(?1\s*,\s*-1\s*,\s*1\)?|k_?1\s*=\s*1)", reason)
                and re.search(r"(?:遗漏|省略).{0,30}(?:β\s*=|beta\s*=|等式左边)", reason, flags=re.IGNORECASE)
                and not re.search(r"(?:系数|结果|符号).{0,40}(?:错误|不正确|不一致)", reason)
            ):
                decision["status"] = "correct"
                continue
            if decision.get("status") == "incorrect" and re.search(
                r"(?:虽然|尽管|虽).{0,240}(?:正确|一致|符合).{0,160}(?:但|不过|然而)",
                reason,
                flags=re.DOTALL,
            ):
                decision["status"] = "partial"
                continue
            # Some responses explicitly acknowledge that the student wrote a
            # required branch conclusion, then still label the entire rubric
            # incorrect because its derivation or formula is incomplete.  The
            # requirement match keeps this repair local to that atomic branch;
            # the student-attribution pattern prevents a quoted reference
            # answer from manufacturing partial credit.
            if decision.get("status") == "incorrect":
                for token in conclusion_tokens:
                    if token not in requirement:
                        continue
                    attributed = re.search(
                        rf"学生.{{0,100}}(?:写出|给出|指出|提及|判断|结论为|总结为).{{0,100}}{re.escape(token)}",
                        reason,
                        flags=re.DOTALL,
                    )
                    if attributed:
                        decision["status"] = "partial"
                        break
        blank_target_pattern = re.compile(
            r"(?:题号|目标题|目标题区域|对应区域|图像中).{0,180}"
            r"(?:无实质作答|未发现.{0,60}作答|没有.{0,60}作答)",
            flags=re.DOTALL,
        )
        blank_decisions = [
            decision
            for decision in rubric_decisions
            if decision.get("status") == "unreadable"
            and blank_target_pattern.search(str(decision.get("reason", "")))
        ]
        if blank_decisions:
            fallback_ref = question_job.roi_refs[0].model_dump(mode="json") if question_job.roi_refs else None
            for decision in blank_decisions:
                decision["status"] = "incorrect"
                if fallback_ref and not decision.get("evidence_refs"):
                    decision["evidence_refs"] = [fallback_ref]
            if all(decision.get("status") == "incorrect" for decision in rubric_decisions):
                payload["verdict"] = QuestionVerdict.INCORRECT.value
                raw_verdict = QuestionVerdict.INCORRECT.value
                if fallback_ref and not evidence_refs:
                    evidence_refs = [fallback_ref]
        # Enforce the grader's own contract when it declares the whole answer
        # correct but leaves otherwise non-contradictory rubric rows unknown.
        # This is a structural repair, not a new mathematical judgment:
        # ``correct`` is defined above as every atomic requirement being met.
        if raw_verdict == QuestionVerdict.CORRECT.value and rubric_decisions and all(
            decision.get("status") in {"correct", "unknown"}
            for decision in rubric_decisions
        ):
            rubric_decisions = [
                {**decision, "status": "correct"}
                if decision.get("status") == "unknown"
                else decision
                for decision in rubric_decisions
            ]
        if not evidence_refs:
            # Preserve the strongest evidence available when Qwen attaches
            # refs only to individual rubric items.
            seen_refs: set[tuple[str, int, tuple[int, int, int, int]]] = set()
            for decision in rubric_decisions:
                for ref in decision.get("evidence_refs", []):
                    ref_value = EvidenceRef.model_validate(ref)
                    key = (ref_value.span_id, ref_value.page, tuple(ref_value.bbox))
                    if key not in seen_refs:
                        evidence_refs.append(ref_value.model_dump(mode="json"))
                        seen_refs.add(key)
        has_rubric_evidence = any(decision.get("evidence_refs") for decision in rubric_decisions)
        strict_rubric_without_evidence = any(
            isinstance(item, dict)
            and "rubric_id" in item
            and str(item.get("status", "")).lower() in {"partial", "incorrect"}
            for item in (raw_rubric_decisions or [])
        ) if isinstance(payload.get("rubric_decisions"), list) else False
        if self.strict_evidence_gate and raw_verdict in {QuestionVerdict.PARTIAL.value, QuestionVerdict.INCORRECT.value} and strict_rubric_without_evidence and not evidence_refs and not has_rubric_evidence:
            raise GradingProviderError(
                f"invalid grading result for {question_job.question_id} (evidence gate)"
            ) from None
        verdict_value = str(payload.get("verdict", "unreadable"))
        if verdict_value not in {item.value for item in QuestionVerdict}:
            verdict_value = QuestionVerdict.UNREADABLE.value
        confidence_was_explicit = payload.get("confidence") not in (None, "")
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence", 0) or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if not confidence_was_explicit and verdict_value != QuestionVerdict.UNREADABLE.value:
            clear_confidences = [span.confidence for span in spans if span.readability == "clear"]
            if clear_confidences and (evidence_refs or any(decision.get("evidence_refs") for decision in rubric_decisions)):
                # This is only a routing confidence fallback.  It does not
                # make a result scoreable without evidence and still leaves
                # the adversarial verifier responsible for high-risk labels.
                confidence = min(0.8, max(clear_confidences))
        risk_value = str(payload.get("risk_level", "low"))
        if risk_value not in {item.value for item in RiskLevel}:
            risk_value = RiskLevel.HIGH.value
        # Never turn an unsupported deduction into a scored result.  This is a
        # safe fallback for legacy Qwen payloads that omit/garble evidence.
        if verdict_value in {QuestionVerdict.PARTIAL.value, QuestionVerdict.INCORRECT.value} and not evidence_refs and not any(
            decision.get("evidence_refs") for decision in rubric_decisions
        ):
            verdict_value = QuestionVerdict.UNREADABLE.value
            confidence = 0.0
            risk_value = RiskLevel.CRITICAL.value
        needs_verification = bool(payload.get("needs_verification", question_job.route != "fast"))
        if verdict_value == QuestionVerdict.UNREADABLE.value:
            needs_verification = True
            if risk_value == RiskLevel.LOW.value:
                risk_value = RiskLevel.HIGH.value
        raw = {
            "question_id": question_job.question_id,
            "verdict": verdict_value,
            "rubric_decisions": rubric_decisions,
            "evidence_refs": evidence_refs,
            "transcription": [span.model_dump(mode="json") for span in spans],
            "confidence": confidence,
            "needs_verification": needs_verification,
            "risk_level": risk_value,
        }
        try:
            return QuestionResult.model_validate(raw)
        except ValueError as exc:
            failure_kind = "evidence gate" if "evidence_refs" in str(exc) else "schema validation"
            raise GradingProviderError(
                f"invalid grading result for {question_job.question_id} ({failure_kind})"
            ) from None

    @staticmethod
    def _valid_evidence_refs(
        raw: Any,
        *,
        question_job: QuestionJob | None = None,
        spans: Sequence[TranscriptionSpan] = (),
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return refs
        span_map = {span.span_id: span for span in spans}
        roi_map = {ref.span_id: ref for ref in (question_job.roi_refs if question_job else [])}
        for item in raw:
            if isinstance(item, str):
                item = {"span_id": item.strip()}
            if not isinstance(item, dict):
                continue
            try:
                EvidenceRef.model_validate(item)
            except ValueError:
                span_id = str(item.get("span_id") or "")
                span = span_map.get(span_id)
                roi = roi_map.get(span_id)
                if span is not None:
                    roi = roi or next((ref for ref in (question_job.roi_refs if question_job else []) if ref.page == span.page), None)
                    if roi is not None:
                        item = {
                            "span_id": span.span_id,
                            "page": span.page,
                            "bbox": list(span.bbox),
                            "artifact_ref": roi.artifact_ref,
                            "view": roi.view,
                        }
                elif roi is not None:
                    item = roi.model_dump(mode="json")
                try:
                    EvidenceRef.model_validate(item)
                except ValueError:
                    continue
            refs.append(item)
        return refs

    @classmethod
    def _valid_rubric_decisions(
        cls,
        raw: Any,
        *,
        question_job: QuestionJob | None = None,
        spans: Sequence[TranscriptionSpan] = (),
    ) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        normalized: list[dict[str, Any]] = []
        status_map = {"met": "correct", "pass": "correct", "not_met": "incorrect", "failed": "incorrect"}
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            rubric_id = str(item.get("rubric_id") or item.get("id") or f"r{index + 1}")
            status = status_map.get(str(item.get("status", "unknown")).lower(), str(item.get("status", "unknown")).lower())
            if status == "not_found":
                status = "unreadable"
            if status in {"met", "pass", "passed", "correct"}:
                status = "correct"
            if status in {"not_met", "failed", "wrong", "error"}:
                status = "incorrect"
            if status not in {"correct", "partial", "incorrect", "unknown", "unreadable"}:
                continue
            raw_refs = item.get("evidence_refs") or item.get("evidence_refs_list") or item.get("evidence_ref")
            if raw_refs is None and item.get("span_id"):
                raw_refs = [item.get("span_id")]
            refs = cls._valid_evidence_refs(raw_refs, question_job=question_job, spans=spans)
            if status in {"partial", "incorrect"} and not refs:
                continue
            reason = item.get("reason") or item.get("comment") or item.get("notes") or item.get("reasoning") or ""
            normalized.append({"rubric_id": rubric_id, "status": status, "evidence_refs": refs, "reason": str(reason)})
        return normalized
