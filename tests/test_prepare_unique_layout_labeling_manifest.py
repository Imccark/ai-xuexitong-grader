from __future__ import annotations

from prepare_unique_layout_labeling_manifest import build_unique_manifest


def test_exact_duplicates_choose_existing_label_as_canonical() -> None:
    rows = [
        {"page_id": "a" * 64, "image_sha256": "1" * 64, "student_hash": "2" * 64},
        {"page_id": "b" * 64, "image_sha256": "1" * 64, "student_hash": "3" * 64},
        {"page_id": "c" * 64, "image_sha256": "4" * 64, "student_hash": "5" * 64},
    ]
    unique, aliases, report = build_unique_manifest(rows, preferred_page_ids={"b" * 64})

    assert {row["page_id"] for row in unique} == {"b" * 64, "c" * 64}
    assert next(row for row in aliases if row["page_id"] == "a" * 64)["canonical_page_id"] == "b" * 64
    assert report["duplicate_alias_pages"] == 1
    assert report["preferred_canonical_pages"] == 1
