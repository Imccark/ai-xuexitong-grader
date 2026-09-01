from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


COLORS = {
    "question_block": "#e53935",
    "subquestion": "#1565c0",
    "student_answer": "#2e7d32",
    "cross_page_continuation": "#f57c00",
    "identity": "#8e24aa",
    "header_footer": "#6d4c41",
    "unknown": "#546e7a",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def render_contact_sheet(
    manifest_path: Path | str,
    results_dir: Path | str,
    dataset_dir: Path | str,
    output_path: Path | str,
    *,
    page_ids: set[str] | None = None,
) -> Path:
    rows = _read_jsonl(Path(manifest_path))
    if page_ids:
        rows = [row for row in rows if str(row.get("page_id")) in page_ids]
    results_root = Path(results_dir)
    dataset_root = Path(dataset_dir)
    tiles: list[Image.Image] = []
    font = ImageFont.load_default(size=18)
    for row in rows:
        page_id = str(row["page_id"])
        result_path = results_root / f"{page_id}.json"
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        image = Image.open(dataset_root / str(result["rectified_image_ref"])).convert("RGB")
        draw = ImageDraw.Draw(image)
        width, height = image.size
        for region in result.get("final_layout", {}).get("regions", []):
            x1, y1, x2, y2 = map(float, region["bbox"])
            box = (int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height))
            color = COLORS.get(str(region.get("region_type")), "#000000")
            draw.rectangle(box, outline=color, width=max(3, width // 500))
            label = str(region.get("question_label") or region.get("region_type") or "region")
            draw.text((box[0] + 3, box[1] + 3), label, fill=color, font=font, stroke_width=2, stroke_fill="white")
        thumbnail = ImageOps.contain(image, (650, 850), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (680, 910), "white")
        tile.paste(thumbnail, ((680 - thumbnail.width) // 2, 45))
        header = (
            f"{page_id[:10]} {result.get('assignment_id', '')} p{result.get('page', 0)} "
            f"{result.get('consensus', {}).get('status', 'unknown')}"
        )
        ImageDraw.Draw(tile).text((12, 10), header, fill="black", font=font)
        tiles.append(tile)
    columns = 2
    rows_count = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * 680, max(1, rows_count) * 910), "#dddddd")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * 680, (index // columns) * 910))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, optimize=True)
    return output.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Render anonymous layout-label QA contact sheets.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--page-id",
        action="append",
        dest="page_ids",
        help="Render only this page id; repeat the option to select multiple pages.",
    )
    args = parser.parse_args()
    output = render_contact_sheet(
        args.manifest,
        args.results_dir,
        args.dataset_dir,
        args.output,
        page_ids=set(args.page_ids or []),
    )
    print(json.dumps({"output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
