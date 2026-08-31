from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .models import (
    Block,
    BlockType,
    ConversionOptions,
    DocumentResult,
    JobState,
    PageResult,
    PageSelection,
    PageType,
    TextQuality,
)

MANIFEST_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1
OCR_CONTRACT_VERSION = "arabic-ocr-v1"
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_JOB_LOGS = 2_000
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class CheckpointError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


class PauseRequested(RuntimeError):
    """Expected control flow raised after a durable page boundary."""


@dataclass(slots=True)
class ConversionControl:
    store: CheckpointStore
    manifest: JobManifest
    pause_requested: Callable[[], bool] = lambda: False

    def page_committed(self, page: PageResult) -> None:
        self.store.write_page(page, self.manifest)
        self.manifest.safe_pages = max(self.manifest.safe_pages, page.page)
        self.manifest.current_page = None
        self.store.write_manifest(self.manifest)
        if self.pause_requested():
            raise PauseRequested


def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise CheckpointError("unsafe_path", "Checkpoint contains an unsafe path")
    return value


def _endpoint_identity(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def processing_contract(
    source_sha256: str,
    page_count: int,
    options: ConversionOptions,
    provider_id: str,
    model: str | None,
    endpoint: str | None = None,
) -> str:
    payload = {
        "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
        "ocr_contract": OCR_CONTRACT_VERSION,
        "source_sha256": source_sha256,
        "page_count": page_count,
        "options": asdict(options),
        "provider_id": provider_id,
        "model": model,
        "endpoint": _endpoint_identity(endpoint),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def page_to_dict(page: PageResult) -> dict[str, Any]:
    value = asdict(page)
    value["page_type"] = page.page_type.value
    value["selection"] = page.selection.value
    for index, block in enumerate(page.blocks):
        value["blocks"][index]["type"] = block.type.value
        value["blocks"][index]["bbox"] = list(block.bbox)
    return value


def page_from_dict(value: dict[str, Any]) -> PageResult:
    try:
        blocks = [
            Block(
                type=BlockType(item["type"]),
                text=str(item.get("text", "")),
                bbox=tuple(float(part) for part in item.get("bbox", [0, 0, 0, 0])),
                confidence=float(item.get("confidence", 1.0)),
                font_size=float(item.get("font_size", 12.0)),
                bold=bool(item.get("bold", False)),
                image_path=(
                    _safe_relative(item["image_path"])
                    if item.get("image_path")
                    else None
                ),
                source=str(item.get("source", "embedded_text")),
                heading_level=int(item.get("heading_level", 1)),
                footnote_id=item.get("footnote_id"),
            )
            for item in value.get("blocks", [])
        ]
        quality = TextQuality(**value.get("quality", {}))
        return PageResult(
            page=int(value["page"]),
            page_type=PageType(value["page_type"]),
            source=str(value["source"]),
            width=float(value["width"]),
            height=float(value["height"]),
            rotation=int(value.get("rotation", 0)),
            blocks=blocks,
            quality=quality,
            ocr_confidence=value.get("ocr_confidence"),
            warnings=list(value.get("warnings", [])),
            selection=PageSelection(value.get("selection", "embedded")),
            selection_reason=str(value.get("selection_reason", "")),
            candidate_scores=dict(value.get("candidate_scores", {})),
            candidates=dict(value.get("candidates", {})),
            ocr_metrics=dict(value.get("ocr_metrics", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointError(
            "invalid_checkpoint", f"Invalid page checkpoint: {exc}"
        ) from exc


@dataclass(slots=True)
class JobManifest:
    id: str
    filename: str
    source_sha256: str
    source_size: int
    page_count: int
    state: JobState
    stage: str
    options: dict[str, Any]
    contract: str
    provider_id: str
    model: str | None = None
    schema_version: int = MANIFEST_SCHEMA_VERSION
    safe_pages: int = 0
    current_page: int | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    paused_at: str | None = None
    completed_at: str | None = None
    recoverable: bool = True
    blocked_reason: str | None = None
    blocked_code: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    log_sequence: int = 0
    lease: dict[str, str] | None = None
    page_durations: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JobManifest:
        if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise CheckpointError("unsupported_schema", "Unsupported job schema")
        if not JOB_ID_RE.fullmatch(str(value.get("id", ""))):
            raise CheckpointError("invalid_job", "Invalid job identifier")
        outputs = {
            str(k): _safe_relative(str(v)) for k, v in value.get("outputs", {}).items()
        }
        try:
            return cls(
                **{
                    **value,
                    "state": JobState(value["state"]),
                    "outputs": outputs,
                    "logs": list(value.get("logs", []))[-MAX_JOB_LOGS:],
                }
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise CheckpointError(
                "invalid_manifest", f"Invalid job manifest: {exc}"
            ) from exc


class CheckpointStore:
    def __init__(self, jobs_root: Path, job_id: str):
        if not JOB_ID_RE.fullmatch(job_id):
            raise CheckpointError("invalid_job", "Invalid job identifier")
        parent = jobs_root.resolve()
        candidate = parent / job_id
        if candidate.exists() and candidate.is_symlink():
            raise CheckpointError("unsafe_path", "Job directory cannot be a symlink")
        self.root = candidate
        self.job_id = job_id
        self.checkpoints = self.root / "checkpoints"
        self.images = self.root / "images"
        self.output = self.root / "output"
        self.work = self.root / "work"

    def initialize(self) -> None:
        for path in (self.checkpoints, self.images, self.output, self.work):
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise CheckpointError("unsafe_path", "Job storage cannot use symlinks")

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def write_manifest(self, manifest: JobManifest) -> None:
        manifest.updated_at = now()
        manifest.logs = manifest.logs[-MAX_JOB_LOGS:]
        self._atomic_json(self.root / "job.json", manifest.to_dict())

    def load_manifest(self) -> JobManifest:
        return JobManifest.from_dict(
            self._read_json(self.root / "job.json", MAX_CHECKPOINT_BYTES)
        )

    def _read_json(self, path: Path, limit: int) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise CheckpointError(
                "corrupt_checkpoint", f"Invalid persisted file: {path.name}"
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                "corrupt_checkpoint", f"Cannot read {path.name}"
            ) from exc
        if not isinstance(value, dict):
            raise CheckpointError("corrupt_checkpoint", f"Invalid {path.name}")
        return value

    def checkpoint_path(self, page: int) -> Path:
        return self.checkpoints / f"page-{page:06d}.json"

    def discard_checkpoints(self) -> None:
        """Drop every saved page so a job can start over from page one."""
        if self.checkpoints.is_dir() and not self.checkpoints.is_symlink():
            for path in sorted(self.checkpoints.glob("page-*.json")):
                path.unlink(missing_ok=True)
        self.initialize()

    def write_page(self, page: PageResult, manifest: JobManifest) -> None:
        for block in page.blocks:
            if block.image_path:
                image = self.images / _safe_relative(block.image_path)
                if image.is_symlink() or not image.is_file():
                    raise CheckpointError(
                        "missing_image", "Referenced image is not durable"
                    )
                with image.open("rb") as handle:
                    os.fsync(handle.fileno())
        try:
            images_fd = os.open(self.images, os.O_RDONLY)
            try:
                os.fsync(images_fd)
            finally:
                os.close(images_fd)
        except OSError:
            pass
        envelope = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "job_id": manifest.id,
            "page": page.page,
            "source_sha256": manifest.source_sha256,
            "contract": manifest.contract,
            "completed_at": now(),
            "result": page_to_dict(page),
        }
        target = self.checkpoint_path(page.page)
        if target.exists():
            if self._read_json(target, MAX_CHECKPOINT_BYTES) != envelope:
                existing = self.load_page(page.page, manifest)
                if page_to_dict(existing) != page_to_dict(page):
                    raise CheckpointError(
                        "checkpoint_conflict", "A different checkpoint already exists"
                    )
            return
        self._atomic_json(target, envelope)

    def load_page(self, number: int, manifest: JobManifest) -> PageResult:
        value = self._read_json(self.checkpoint_path(number), MAX_CHECKPOINT_BYTES)
        if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError("unsupported_schema", "Unsupported checkpoint schema")
        if value.get("job_id") != manifest.id or value.get("page") != number:
            raise CheckpointError("corrupt_checkpoint", "Checkpoint identity mismatch")
        if (
            value.get("source_sha256") != manifest.source_sha256
            or value.get("contract") != manifest.contract
        ):
            raise CheckpointError(
                "contract_mismatch", "Checkpoint processing contract changed"
            )
        page = page_from_dict(value.get("result", {}))
        if page.page != number:
            raise CheckpointError("corrupt_checkpoint", "Checkpoint page mismatch")
        return page

    def contiguous_pages(self, manifest: JobManifest) -> list[PageResult]:
        pages = []
        for number in range(1, manifest.page_count + 1):
            target = self.checkpoint_path(number)
            if not target.exists():
                break
            try:
                pages.append(self.load_page(number, manifest))
            except CheckpointError:
                if number <= manifest.safe_pages:
                    raise
                quarantine = self.checkpoints / (
                    f".corrupt-page-{number:06d}-{uuid4().hex[:12]}.json"
                )
                os.replace(target, quarantine)
                try:
                    directory_fd = os.open(self.checkpoints, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
                break
        if manifest.safe_pages > len(pages):
            raise CheckpointError(
                "corrupt_safe_prefix", "A durable checkpoint is corrupt or missing"
            )
        if manifest.safe_pages != len(pages):
            manifest.safe_pages = len(pages)
            self.write_manifest(manifest)
        return pages

    def reconstruct(self, manifest: JobManifest) -> DocumentResult:
        pages = self.contiguous_pages(manifest)
        document = DocumentResult(
            Path(manifest.filename).stem,
            fix_bidi=bool(manifest.options.get("fix_bidi", True)),
        )
        document.pages.extend(pages)
        for page in pages:
            for block in page.blocks:
                if block.image_path:
                    document.images.append(self.images / block.image_path)
        return document
