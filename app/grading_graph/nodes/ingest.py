from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class IngestLimits:
    max_zip_depth: int = 2
    max_files: int = 2000
    max_uncompressed_bytes: int = 500_000_000
    max_member_bytes: int = 120_000_000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or PureWindowsPath(normalized).drive:
        raise ValueError(f"path traversal in archive member: {name}")
    # Inspect the original components before normpath can erase a traversal
    # such as ``folder/../outside.png``.
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        raise ValueError(f"path traversal in archive member: {name}")
    return posixpath.normpath(normalized)


def _kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix == ".docx":
        return "docx"
    if suffix == ".zip":
        return "archive"
    return "other"


def _archive_entries_from_bytes(
    data: bytes,
    limits: IngestLimits,
    *,
    depth: int,
    prefix: str,
    counters: dict[str, int],
) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid zip archive") from exc
    entries: list[dict[str, Any]] = []
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        for info in infos:
            name = _safe_member_name(info.filename)
            display_name = f"{prefix}{name}"
            counters["files"] += 1
            if counters["files"] > limits.max_files:
                raise ValueError(f"file count exceeds limit: {counters['files']} > {limits.max_files}")
            if info.file_size > limits.max_member_bytes:
                raise ValueError(f"member exceeds size limit: {display_name}")
            counters["bytes"] += info.file_size
            if counters["bytes"] > limits.max_uncompressed_bytes:
                raise ValueError("uncompressed archive size exceeds limit")
            kind = _kind(display_name)
            member_data = archive.read(info)
            entries.append(
                {
                    "name": display_name,
                    "kind": kind,
                    "size_bytes": info.file_size,
                    "sha256": _sha256(member_data),
                    "nested_archive": kind == "archive",
                }
            )
            if kind == "archive":
                if depth >= limits.max_zip_depth:
                    raise ValueError("nested archive depth exceeds limit")
                entries.extend(
                    _archive_entries_from_bytes(
                        member_data,
                        limits,
                        depth=depth + 1,
                        prefix=f"{display_name}/",
                        counters=counters,
                    )
                )
    return entries


def _archive_entries(archive_path: Path, limits: IngestLimits) -> list[dict[str, Any]]:
    return _archive_entries_from_bytes(
        archive_path.read_bytes(),
        limits,
        depth=0,
        prefix="",
        counters={"files": 0, "bytes": 0},
    )


def build_ingest_manifest(source_path: Path | str, *, limits: IngestLimits | None = None) -> dict[str, Any]:
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    limits = limits or IngestLimits()
    source_hash = _sha256(source.read_bytes())
    if source.suffix.lower() == ".zip":
        files = _archive_entries(source, limits)
        source_type = "zip"
    else:
        data = source.read_bytes()
        if len(data) > limits.max_member_bytes:
            raise ValueError("source exceeds size limit")
        files = [{
            "name": source.name,
            "kind": _kind(source.name),
            "size_bytes": len(data),
            "sha256": _sha256(data),
            "nested_archive": False,
        }]
        source_type = _kind(source.name)
    supported = [entry for entry in files if entry["kind"] in {"pdf", "image", "docx"}]
    return {
        "source_sha256": source_hash,
        "source_type": source_type,
        "files": files,
        "supported_file_count": len(supported),
        "status": "ready" if supported else "unsupported",
        "limits": {
            "max_zip_depth": limits.max_zip_depth,
            "max_files": limits.max_files,
            "max_uncompressed_bytes": limits.max_uncompressed_bytes,
            "max_member_bytes": limits.max_member_bytes,
        },
    }
