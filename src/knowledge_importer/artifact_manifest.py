"""Deterministic manifest for PDF and Markdown artifacts produced by one run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from knowledge_importer import __version__
from knowledge_importer.json_writer import write_json_atomically

_HASH_CHUNK_SIZE = 1024 * 1024


class ManifestStatus(Enum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    bytes: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class ArtifactManifestItem:
    input_path: str
    output_path: str
    status: ManifestStatus
    input_digest: ArtifactDigest
    output_digest: ArtifactDigest
    error_category: str | None = None
    message: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "input": {
                "path": self.input_path,
                "bytes": self.input_digest.bytes,
                "sha256": self.input_digest.sha256,
            },
            "output": {
                "path": self.output_path,
                "bytes": self.output_digest.bytes,
                "sha256": self.output_digest.sha256,
            },
            "status": self.status.value,
            "error_category": self.error_category,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ArtifactManifestSettings:
    recursive: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    force: bool = False
    table_structure: bool = False
    artifacts_path_configured: bool = False
    normalization_profile: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "recursive": self.recursive,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "force": self.force,
            "table_structure": self.table_structure,
            "artifacts_path_configured": self.artifacts_path_configured,
            "normalization_profile": self.normalization_profile,
        }


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    settings: ArtifactManifestSettings
    items: tuple[ArtifactManifestItem, ...]

    def payload(self) -> dict[str, object]:
        succeeded = sum(item.status is ManifestStatus.SUCCEEDED for item in self.items)
        skipped = sum(item.status is ManifestStatus.SKIPPED for item in self.items)
        failed = sum(item.status is ManifestStatus.FAILED for item in self.items)
        return {
            "report_type": "knowledge-artifact-manifest",
            "schema_version": 1,
            "engine": {
                "name": "knowledge-importer",
                "version": __version__,
            },
            "settings": self.settings.payload(),
            "summary": {
                "items": len(self.items),
                "succeeded": succeeded,
                "skipped": skipped,
                "failed": failed,
            },
            "items": [item.payload() for item in self.items],
        }


def digest_file(path: Path) -> ArtifactDigest:
    """Hash file bytes with bounded memory use."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as artifact:
        while chunk := artifact.read(_HASH_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return ArtifactDigest(bytes=size, sha256=digest.hexdigest())


def _optional_digest(path: Path) -> ArtifactDigest:
    try:
        return digest_file(path)
    except OSError:
        return ArtifactDigest(bytes=None, sha256=None)


def build_manifest_item(
    *,
    input_path: Path,
    output_path: Path,
    input_name: str,
    output_name: str,
    status: ManifestStatus,
    error_category: str | None = None,
    message: str | None = None,
) -> ArtifactManifestItem:
    """Build one item; successful and skipped artifacts require complete digests."""
    if status is ManifestStatus.FAILED:
        input_digest = _optional_digest(input_path)
        output_digest = ArtifactDigest(bytes=None, sha256=None)
    else:
        input_digest = digest_file(input_path)
        output_digest = digest_file(output_path)
    return ArtifactManifestItem(
        input_path=input_name,
        output_path=output_name,
        status=status,
        input_digest=input_digest,
        output_digest=output_digest,
        error_category=error_category,
        message=message,
    )


def write_artifact_manifest(report_path: Path, manifest: ArtifactManifest) -> None:
    """Write Manifest schema v1 atomically using the shared JSON writer."""
    write_json_atomically(report_path, manifest.payload())
