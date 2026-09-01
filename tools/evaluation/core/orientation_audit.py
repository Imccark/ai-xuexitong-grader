from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from app.grading_graph.nodes.orientation import classify_document_orientation
from app.grading_graph.store import atomic_write_json


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contact_sheet(rows: list[dict[str, Any]], image_root: Path, output_path: Path) -> None:
    if not rows:
        return
    tile_width, tile_height, label_height = 320, 420, 34
    columns = min(4, len(rows))
    lines = (len(rows) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile_width, lines * (tile_height + label_height)), "#202124")
    draw = ImageDraw.Draw(sheet)
    for index, row in enumerate(rows):
        image = Image.open(image_root / Path(str(row["rectified_image_ref"])).name).convert("RGB")
        thumbnail = ImageOps.contain(image, (tile_width - 8, tile_height - 8), Image.Resampling.LANCZOS)
        x = (index % columns) * tile_width + (tile_width - thumbnail.width) // 2
        y = (index // columns) * (tile_height + label_height) + (tile_height - thumbnail.height) // 2
        sheet.paste(thumbnail, (x, y))
        label = (
            f"{str(row['page_id'])[:10]}  rotate={row.get('rotation_degrees_clockwise', 0)}  "
            f"conf={float(row.get('orientation_confidence', 0.0)):.3f}"
        )
        draw.text(((index % columns) * tile_width + 6, (index // columns) * (tile_height + label_height) + tile_height + 8), label, fill="white")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG", optimize=True)


def audit_orientation_dataset(manifest_path: Path | str, output_dir: Path | str) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    output_dir = Path(output_dir).resolve()
    rows = _read_jsonl(manifest_path)
    image_root = manifest_path.parent / "images"
    residual_nonzero: list[dict[str, Any]] = []
    residual_ambiguous: list[dict[str, Any]] = []
    suppressed_unstable: list[dict[str, Any]] = []
    for row in rows:
        image_path = image_root / Path(str(row["rectified_image_ref"])).name
        with Image.open(image_path) as image:
            result = classify_document_orientation(image)
        residual = {
            "page_id": row["page_id"],
            "predicted_orientation_degrees_clockwise": result.get("predicted_orientation_degrees_clockwise", 0),
            "confidence": result.get("confidence", 0.0),
            "margin": result.get("margin", 0.0),
            "reason": result.get("reason", "unknown"),
        }
        orientation_status = ((row.get("geometry") or {}).get("orientation") or {}).get("reason")
        if result.get("applied"):
            if orientation_status == "unstable_orientation_cycle":
                suppressed_unstable.append(residual)
            else:
                residual_nonzero.append(residual)
        elif result.get("reason") == "low_orientation_confidence":
            residual_ambiguous.append(residual)

    rotated_rows = [row for row in rows if int(row.get("rotation_degrees_clockwise", 0) or 0) != 0]
    ambiguous_rows = [row for row in rows if row.get("orientation_status") == "low_orientation_confidence"]
    candidate_search_rows = [
        row
        for row in rows
        if ((row.get("geometry") or {}).get("orientation") or {}).get("candidate_search") is not None
    ]
    candidate_search_resolved_rows = [
        row
        for row in candidate_search_rows
        if bool((((row.get("geometry") or {}).get("orientation") or {}).get("candidate_search") or {}).get("accepted"))
    ]
    _contact_sheet(rotated_rows, image_root, output_dir / "rotated_pages_contact_sheet.png")
    _contact_sheet(ambiguous_rows, image_root, output_dir / "ambiguous_pages_contact_sheet.png")
    report = {
        "schema_version": "1.0",
        "pages": len(rows),
        "rotated_pages": len(rotated_rows),
        "preprocess_ambiguous_pages": len(ambiguous_rows),
        "candidate_search_pages": len(candidate_search_rows),
        "candidate_search_resolved_pages": len(candidate_search_resolved_rows),
        "residual_nonzero_pages": len(residual_nonzero),
        "residual_ambiguous_pages": len(residual_ambiguous),
        "suppressed_unstable_pages": len(suppressed_unstable),
        "residual_nonzero": residual_nonzero,
        "residual_ambiguous": residual_ambiguous,
        "suppressed_unstable": suppressed_unstable,
        "rotated_contact_sheet": str(output_dir / "rotated_pages_contact_sheet.png"),
        "ambiguous_contact_sheet": str(output_dir / "ambiguous_pages_contact_sheet.png"),
    }
    atomic_write_json(output_dir / "orientation_audit.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit canonical orientation after document preprocessing.")
    parser.add_argument("--manifest", default="datasets/layout_all_v4/manifest.jsonl")
    parser.add_argument("--output-dir", default="datasets/layout_all_v4/qa")
    args = parser.parse_args()
    report = audit_orientation_dataset(args.manifest, args.output_dir)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["residual_nonzero_pages"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
