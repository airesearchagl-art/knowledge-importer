"""Deterministic per-document metadata sidecars."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from knowledge_importer import __version__
from knowledge_importer.artifact_manifest import ArtifactDigest, ArtifactManifestItem
from knowledge_importer.json_writer import write_json_atomically


@dataclass(frozen=True, slots=True)
class DocumentMetadataSettings:
    table_structure: bool
    normalization_profile: str | None
    artifacts_path_configured: bool

    def payload(self) -> dict[str, object]:
        return {
            "table_structure": self.table_structure,
            "normalization_profile": self.normalization_profile,
            "artifacts_path_configured": self.artifacts_path_configured,
        }


@dataclass(frozen=True, slots=True)
class DocumentMetadataSidecar:
    input_path: str
    output_path: str
    status: str
    artifact: ArtifactDigest
    source: ArtifactDigest
    settings: DocumentMetadataSettings

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-document-metadata",
            "schema_version": 1,
            "engine": {
                "name": "knowledge-importer",
                "version": __version__,
            },
            "document": {
                "input_path": self.input_path,
                "output_path": self.output_path,
                "status": self.status,
            },
            "artifact": {
                "bytes": self.artifact.bytes,
                "sha256": self.artifact.sha256,
            },
            "source": {
                "bytes": self.source.bytes,
                "sha256": self.source.sha256,
            },
            "settings": self.settings.payload(),
        }


def metadata_sidecar_path(markdown_path: Path) -> Path:
    """Return the adjacent sidecar path for one Markdown output."""

    return markdown_path.with_suffix(".metadata.json")


def build_document_metadata(
    item: ArtifactManifestItem,
    settings: DocumentMetadataSettings,
) -> DocumentMetadataSidecar:
    """Build metadata from an already-digested Manifest-compatible artifact item."""

    if item.input_digest.bytes is None or item.input_digest.sha256 is None:
        raise ValueError("source digest is incomplete")
    if item.output_digest.bytes is None or item.output_digest.sha256 is None:
        raise ValueError("artifact digest is incomplete")
    return DocumentMetadataSidecar(
        input_path=item.input_path,
        output_path=item.output_path,
        status=item.status.value,
        artifact=item.output_digest,
        source=item.input_digest,
        settings=settings,
    )


def write_document_metadata(path: Path, sidecar: DocumentMetadataSidecar) -> None:
    """Write metadata sidecar schema v1 atomically."""

    write_json_atomically(path, sidecar.payload())
