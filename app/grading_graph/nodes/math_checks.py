from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

from sympy import Matrix

from app.grading_graph.schemas import QuestionJob, QuestionResult, RiskLevel


_NUMERIC_EQUATION = re.compile(
    r"(?P<left>[0-9().+*/\s-]+)=(?P<right>[0-9().+*/\s-]+)"
)
_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_MATRIX_KIND = r"(?:b|p|B|P|v|V)?matrix"
_MATRIX_LITERAL = rf"(?:\[\s*\[.*?\]\s*\]|\\begin\{{{_MATRIX_KIND}\}}.*?\\end\{{{_MATRIX_KIND}\}})"
_MATRIX_EQUATION = re.compile(
    rf"(?P<left>{_MATRIX_LITERAL})\s*=\s*(?P<right>{_MATRIX_LITERAL})",
    re.DOTALL,
)
_MATRIX_PROPERTY = re.compile(
    rf"(?P<operation>trace|tr|det|determinant)\s*\(\s*(?P<matrix>{_MATRIX_LITERAL})\s*\)\s*=\s*(?P<value>[0-9().+*/\\s-]+)",
    re.IGNORECASE | re.DOTALL,
)


def _safe_numeric_expression(expression: str) -> float | None:
    """Evaluate only numeric arithmetic; never evaluate provider text as code."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            value = float(node.value)
            if not math.isfinite(value):
                raise ValueError
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError
            value = _BINARY_OPERATORS[type(node.op)](left, right)
            if not math.isfinite(value):
                raise ValueError
            return value
        raise ValueError

    try:
        return visit(tree.body)
    except (ArithmeticError, ValueError, OverflowError):
        return None


def check_numeric_equations(text: str, *, tolerance: float = 1e-9) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for match in _NUMERIC_EQUATION.finditer(text or ""):
        left_text = match.group("left").strip()
        right_text = match.group("right").strip()
        left = _safe_numeric_expression(left_text)
        right = _safe_numeric_expression(right_text)
        if left is None or right is None:
            continue
        checks.append(
            {
                "expression": f"{left_text}={right_text}",
                "consistent": math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance),
            }
        )
    return checks


def _parse_numeric_matrix(text: str) -> Matrix | None:
    """Parse only numeric matrix literals; never execute provider-controlled text."""
    value = (text or "").replace(r"\left", "").replace(r"\right", "").strip()
    begin = re.fullmatch(rf"\\begin\{{(?P<kind>{_MATRIX_KIND})\}}(?P<body>.*?)\\end\{{(?P=kind)\}}", value, re.DOTALL)
    if begin:
        raw_rows = re.split(r"\\\\|;", begin.group("body"))
    else:
        row_matches = re.findall(r"\[([^\[\]]+)\]", value, re.DOTALL)
        if not row_matches or not value.startswith("[") or not value.endswith("]"):
            return None
        raw_rows = row_matches

    rows: list[list[float]] = []
    for raw_row in raw_rows:
        cells = [cell.strip() for cell in re.split(r"\s*(?:&|,)\s*", raw_row) if cell.strip()]
        if not cells:
            return None
        parsed_row: list[float] = []
        for cell in cells:
            number = _safe_numeric_expression(cell)
            if number is None:
                return None
            parsed_row.append(number)
        rows.append(parsed_row)
    if not rows or len({len(row) for row in rows}) != 1:
        return None
    try:
        return Matrix(rows)
    except (TypeError, ValueError):
        return None


def _matrix_shape(matrix: Matrix) -> list[int]:
    return [int(matrix.rows), int(matrix.cols)]


def check_matrix_calculations(text: str, *, tolerance: float = 1e-9) -> list[dict[str, Any]]:
    """Check simple numeric matrix equations, traces, and determinants with SymPy."""
    normalized = (text or "").replace(r"\left", "").replace(r"\right", "")
    checks: list[dict[str, Any]] = []
    for match in _MATRIX_EQUATION.finditer(normalized):
        left = _parse_numeric_matrix(match.group("left"))
        right = _parse_numeric_matrix(match.group("right"))
        if left is None or right is None:
            continue
        same_shape = left.shape == right.shape
        consistent = bool(same_shape and left.equals(right))
        checks.append(
            {
                "kind": "matrix_equation",
                "left_shape": _matrix_shape(left),
                "right_shape": _matrix_shape(right),
                "consistent": consistent,
            }
        )

    for match in _MATRIX_PROPERTY.finditer(normalized):
        matrix = _parse_numeric_matrix(match.group("matrix"))
        expected = _safe_numeric_expression(match.group("value"))
        if matrix is None or expected is None:
            continue
        operation = match.group("operation").lower()
        is_square = matrix.rows == matrix.cols
        if not is_square:
            checks.append(
                {
                    "kind": f"matrix_{operation}",
                    "shape": _matrix_shape(matrix),
                    "consistent": False,
                    "reason": "non_square_matrix",
                }
            )
            continue
        calculated = matrix.trace() if operation in {"trace", "tr"} else matrix.det()
        try:
            consistent = math.isclose(float(calculated), expected, rel_tol=tolerance, abs_tol=tolerance)
        except (TypeError, ValueError, OverflowError):
            continue
        checks.append(
            {
                "kind": f"matrix_{operation}",
                "shape": _matrix_shape(matrix),
                "expected": expected,
                "consistent": consistent,
            }
        )
    return checks


def apply_deterministic_math_checks(job: QuestionJob, result: QuestionResult) -> QuestionResult:
    checks: list[dict[str, Any]] = []
    for span in result.transcription:
        checks.extend(check_numeric_equations(span.text))
        checks.extend(check_matrix_calculations(span.text))
    conflicts = [item for item in checks if not item["consistent"]]
    if not conflicts:
        return result
    return result.model_copy(
        update={
            "needs_verification": True,
            "risk_level": RiskLevel.HIGH,
            "verifier_result": {
                "decisive": False,
                "deterministic_checks": checks,
                "reason": "numeric equation is internally inconsistent",
            },
        }
    )
