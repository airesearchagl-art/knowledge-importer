"""Backup Session Manifest v1 and read-only Backup Inventory v1."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol

from knowledge_importer.artifact_manifest import ArtifactDigest
from knowledge_importer.json_writer import write_json_atomically

SESSION_MANIFEST_FILENAME = "session-manifest.json"
MANAGED_SESSION_PREFIX = "knowledge-importer-repair-v1-"
LEGACY_SESSION_PREFIX = "knowledge-importer-repair-"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SESSION_NAME = re.compile(r"^knowledge-importer-repair-v1-[A-Za-z0-9_-]+$")
_BACKUP_INDEX = re.compile(r"^[0-9]{4}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_READ_CHUNK_SIZE = 1024 * 1024


class BackupInventoryInputError(ValueError):
    """Raised when an inventory root cannot be inspected safely."""


class BackupSessionBindingError(ValueError):
    """Raised when otherwise valid session metadata has unverifiable bindings."""


class BackupSessionState(str, Enum):
    OPEN = "open"
    COMPLETE = "complete"
    ROLLED_BACK = "rolled-back"
    ROLLBACK_FAILED = "rollback-failed"


class BackupSessionClassification(str, Enum):
    MANAGED = "managed"
    MISSING_MANIFEST = "missing-session-manifest"
    INVALID_MANIFEST = "invalid-session-manifest"
    INTERRUPTED = "interrupted-open-session"
    UNEXPECTED_ENTRY = "unexpected-entry"
    BINDING_UNVERIFIABLE = "binding-unverifiable"
    LEGACY_UNMANAGED = "legacy-unmanaged"


class _HashWriter(Protocol):
    def update(self, content: bytes) -> object: ...


@dataclass(frozen=True, slots=True)
class BackupSessionBindings:
    manifest_sha256: str
    plan_sha256: str
    approval_sha256: str
    preflight_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "manifest": {"sha256": self.manifest_sha256, "schema_version": 1},
            "plan": {"sha256": self.plan_sha256, "schema_version": 1},
            "approval": {"sha256": self.approval_sha256, "schema_version": 1},
            "preflight": {"sha256": self.preflight_sha256, "schema_version": 1},
        }


@dataclass(frozen=True, slots=True)
class BackupSessionItem:
    source: str
    backup: str
    digest: ArtifactDigest

    def payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "backup": self.backup,
            "bytes": self.digest.bytes,
            "sha256": self.digest.sha256,
        }


@dataclass(frozen=True, slots=True)
class BackupSessionManifest:
    state: BackupSessionState
    bindings: BackupSessionBindings
    items: tuple[BackupSessionItem, ...]

    def payload(self) -> dict[str, object]:
        return {
            "report_type": "knowledge-importer-repair-backup-session",
            "schema_version": 1,
            "state": self.state.value,
            "bindings": self.bindings.payload(),
            "items": [item.payload() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class BackupInventorySession:
    session: str
    classification: BackupSessionClassification
    state: BackupSessionState | None
    planning_eligible: bool
    session_manifest_sha256: str | None
    tree_sha256: str | None
    items: tuple[BackupSessionItem, ...]

    def payload(self) -> dict[str, object]:
        return {
            "session": self.session,
            "classification": self.classification.value,
            "state": self.state.value if self.state is not None else None,
            "planning_eligible": self.planning_eligible,
            "session_manifest_sha256": self.session_manifest_sha256,
            "tree_sha256": self.tree_sha256,
            "items": [item.payload() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class BackupInventory:
    sessions: tuple[BackupInventorySession, ...]

    @property
    def exit_code(self) -> int:
        unhealthy = {
            BackupSessionClassification.MISSING_MANIFEST,
            BackupSessionClassification.INVALID_MANIFEST,
            BackupSessionClassification.INTERRUPTED,
            BackupSessionClassification.UNEXPECTED_ENTRY,
            BackupSessionClassification.BINDING_UNVERIFIABLE,
            BackupSessionClassification.LEGACY_UNMANAGED,
        }
        return (
            1
            if any(
                item.classification in unhealthy or item.state is BackupSessionState.ROLLBACK_FAILED
                for item in self.sessions
            )
            else 0
        )

    def payload(self) -> dict[str, object]:
        classifications = [session.classification for session in self.sessions]
        return {
            "report_type": "knowledge-importer-backup-inventory",
            "schema_version": 1,
            "summary": {
                "sessions": len(self.sessions),
                "managed": classifications.count(BackupSessionClassification.MANAGED),
                "orphaned": sum(
                    classification
                    not in {
                        BackupSessionClassification.MANAGED,
                        BackupSessionClassification.LEGACY_UNMANAGED,
                    }
                    for classification in classifications
                ),
                "legacy_unmanaged": classifications.count(
                    BackupSessionClassification.LEGACY_UNMANAGED
                ),
                "planning_eligible": sum(session.planning_eligible for session in self.sessions),
                "backup_files": sum(len(session.items) for session in self.sessions),
                "backup_bytes": sum(
                    item.digest.bytes or 0 for session in self.sessions for item in session.items
                ),
            },
            "sessions": [session.payload() for session in self.sessions],
        }


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_relative_posix_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or _WINDOWS_DRIVE.match(value)
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _sort_key(value: str) -> tuple[str, str]:
    return (unicodedata.normalize("NFC", value).casefold(), value)


def _comparison_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _parse_bindings(value: object) -> BackupSessionBindings:
    if not isinstance(value, dict):
        raise BackupSessionBindingError("invalid backup session bindings")
    parsed: list[str] = []
    for name in ("manifest", "plan", "approval", "preflight"):
        binding = value.get(name)
        if not (
            isinstance(binding, dict)
            and binding.get("schema_version") == 1
            and not isinstance(binding.get("schema_version"), bool)
            and _is_sha256(binding.get("sha256"))
        ):
            raise BackupSessionBindingError("invalid backup session bindings")
        parsed.append(binding["sha256"])
    return BackupSessionBindings(*parsed)


def _parse_session_item(value: object) -> BackupSessionItem:
    if not isinstance(value, dict):
        raise ValueError("invalid backup session item")
    source = value.get("source")
    backup = value.get("backup")
    size = value.get("bytes")
    sha256 = value.get("sha256")
    if not (
        _is_relative_posix_path(source)
        and _is_relative_posix_path(backup)
        and _is_nonnegative_int(size)
        and _is_sha256(sha256)
    ):
        raise ValueError("invalid backup session item")
    assert isinstance(source, str)
    assert isinstance(backup, str)
    backup_path = PurePosixPath(backup)
    if not (
        source.casefold().endswith(".metadata.json")
        and len(backup_path.parts) >= 2
        and _BACKUP_INDEX.fullmatch(backup_path.parts[0]) is not None
        and PurePosixPath(*backup_path.parts[1:]).as_posix() == f"{source}.bak"
        and backup != SESSION_MANIFEST_FILENAME
    ):
        raise ValueError("invalid backup session item semantics")
    return BackupSessionItem(source, backup, ArtifactDigest(size, sha256))


def parse_backup_session_manifest_bytes(content: bytes) -> BackupSessionManifest:
    """Parse and strictly validate Backup Session Manifest schema v1."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Backup Session Manifest JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Backup Session Manifest root")
    schema_version = payload.get("schema_version")
    state = payload.get("state")
    raw_items = payload.get("items")
    if not (
        payload.get("report_type") == "knowledge-importer-repair-backup-session"
        and schema_version == 1
        and not isinstance(schema_version, bool)
        and state in {candidate.value for candidate in BackupSessionState}
        and isinstance(raw_items, list)
    ):
        raise ValueError("invalid Backup Session Manifest schema")
    bindings = _parse_bindings(payload.get("bindings"))
    items = tuple(_parse_session_item(item) for item in raw_items)
    sources = [item.source for item in items]
    backups = [item.backup for item in items]
    if len({_comparison_key(value) for value in sources}) != len(sources) or len(
        {_comparison_key(value) for value in backups}
    ) != len(backups):
        raise ValueError("duplicate Backup Session Manifest item")
    if list(items) != sorted(items, key=lambda item: _sort_key(item.backup)):
        raise ValueError("non-deterministic Backup Session Manifest item order")
    return BackupSessionManifest(BackupSessionState(state), bindings, items)


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_new_file_no_clobber(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    if path.exists() or is_link_or_reparse(path):
        raise FileExistsError
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def write_backup_session_manifest(
    path: Path,
    manifest: BackupSessionManifest,
    *,
    expected_current: BackupSessionManifest | None,
) -> None:
    """Create or atomically update a session manifest without accepting foreign content."""

    content = _canonical_json_bytes(manifest.payload())
    if expected_current is None:
        _write_new_file_no_clobber(path, content)
        return
    if is_link_or_reparse(path) or not path.is_file():
        raise OSError("unsafe backup session manifest")
    current_bytes = path.read_bytes()
    current = parse_backup_session_manifest_bytes(current_bytes)
    if current != expected_current or current_bytes != _canonical_json_bytes(current.payload()):
        raise OSError("backup session manifest changed")
    write_json_atomically(path, manifest.payload())


def transition_backup_session(
    manifest: BackupSessionManifest,
    state: BackupSessionState,
) -> BackupSessionManifest:
    """Apply one valid state transition to a session manifest."""

    allowed = {
        BackupSessionState.OPEN: {
            BackupSessionState.OPEN,
            BackupSessionState.COMPLETE,
            BackupSessionState.ROLLED_BACK,
            BackupSessionState.ROLLBACK_FAILED,
        },
        BackupSessionState.COMPLETE: set(),
        BackupSessionState.ROLLED_BACK: set(),
        BackupSessionState.ROLLBACK_FAILED: set(),
    }
    if state not in allowed[manifest.state]:
        raise ValueError("invalid backup session state transition")
    return replace(manifest, state=state)


def is_link_or_reparse(path: Path) -> bool:
    """Return whether a path is a symlink, junction, or other reparse point."""

    try:
        metadata = path.lstat()
    except OSError:
        return path.is_symlink()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _safe_existing_directory(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not current.exists() or is_link_or_reparse(current) or not current.is_dir():
            return False
    return True


def path_uses_link_or_reparse(path: Path) -> bool:
    """Check every existing component without following a link or reparse point."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if is_link_or_reparse(current):
                return True
        else:
            break
    return False


def repository_roots(package_root: Path) -> tuple[Path, ...]:
    """Find the nearest repository roots relevant to package and process context."""

    roots: set[Path] = set()
    for start in (package_root.absolute(), Path.cwd().absolute()):
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                roots.add(candidate.resolve())
                break
    return tuple(sorted(roots, key=lambda path: _sort_key(path.as_posix())))


def validate_backup_root(package_root: Path, backup_root: Path) -> None:
    """Require safe, non-overlapping existing package and backup directories."""

    if not _safe_existing_directory(package_root) or not _safe_existing_directory(backup_root):
        raise BackupInventoryInputError("unsafe backup inventory root")
    package_resolved = package_root.resolve()
    backup_resolved = backup_root.resolve()
    forbidden = (package_resolved, *repository_roots(package_root))
    if any(backup_resolved == item or backup_resolved.is_relative_to(item) for item in forbidden):
        raise BackupInventoryInputError("backup root overlaps package or repository")


def path_is_within(path: Path, root: Path) -> bool:
    """Compare paths without requiring the candidate to exist."""

    candidate = path.resolve(strict=False)
    resolved_root = root.resolve()
    return candidate == resolved_root or candidate.is_relative_to(resolved_root)


def _safe_session_reference(name: str) -> str:
    if _SESSION_NAME.fullmatch(name):
        return name
    digest = hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"invalid-session-{digest}"


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _read_regular_file(path: Path) -> bytes:
    if is_link_or_reparse(path):
        raise OSError("unsafe managed backup file")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("unsafe managed backup file")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if _stable_file_identity(opened) != _stable_file_identity(before):
            raise OSError("managed backup file changed")
        content = source.read()
    after = path.lstat()
    if is_link_or_reparse(path) or not stat.S_ISREG(after.st_mode):
        raise OSError("unsafe managed backup file")
    if _stable_file_identity(after) != _stable_file_identity(before):
        raise OSError("managed backup file changed")
    return content


def _hash_file_and_tree(
    tree: _HashWriter,
    relative: str,
    path: Path,
) -> ArtifactDigest:
    if is_link_or_reparse(path):
        raise OSError("unsafe managed backup file")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("unsafe managed backup file")
    path_bytes = relative.encode("utf-8")
    tree.update(b"file\x00")
    tree.update(len(path_bytes).to_bytes(8, "big"))
    tree.update(path_bytes)
    tree.update(before.st_size.to_bytes(8, "big"))
    digest = hashlib.sha256()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if _stable_file_identity(opened) != _stable_file_identity(before):
            raise OSError("managed backup file changed")
        while chunk := source.read(_READ_CHUNK_SIZE):
            digest.update(chunk)
            tree.update(chunk)
    after = path.lstat()
    if is_link_or_reparse(path) or not stat.S_ISREG(after.st_mode):
        raise OSError("unsafe managed backup file")
    if _stable_file_identity(after) != _stable_file_identity(before):
        raise OSError("managed backup file changed")
    return ArtifactDigest(before.st_size, digest.hexdigest())


def _actual_session_tree(session: Path) -> tuple[set[str], set[str], bool]:
    files: set[str] = set()
    directories: set[str] = set()
    unsafe = False
    pending: list[tuple[Path, PurePosixPath]] = [(session, PurePosixPath())]
    while pending:
        directory, relative_parent = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: _sort_key(entry.name))
        except OSError:
            return files, directories, True
        for entry in entries:
            relative = relative_parent / entry.name
            relative_name = relative.as_posix()
            path = Path(entry.path)
            if is_link_or_reparse(path):
                unsafe = True
            elif entry.is_dir(follow_symlinks=False):
                directories.add(relative_name)
                pending.append((path, relative))
            elif entry.is_file(follow_symlinks=False):
                files.add(relative_name)
            else:
                unsafe = True
    return files, directories, unsafe


def _expected_directories(items: tuple[BackupSessionItem, ...]) -> set[str]:
    directories: set[str] = set()
    for item in items:
        parent = PurePosixPath(item.backup).parent
        while parent.parts:
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _invalid_session(
    session_name: str,
    classification: BackupSessionClassification,
    *,
    state: BackupSessionState | None = None,
) -> BackupInventorySession:
    return BackupInventorySession(
        _safe_session_reference(session_name),
        classification,
        state,
        False,
        None,
        None,
        (),
    )


def _inspect_managed_session(session: Path) -> BackupInventorySession:
    name = session.name
    if not _SESSION_NAME.fullmatch(name) or is_link_or_reparse(session) or not session.is_dir():
        return _invalid_session(name, BackupSessionClassification.UNEXPECTED_ENTRY)
    manifest_path = session / SESSION_MANIFEST_FILENAME
    if not manifest_path.exists():
        return _invalid_session(name, BackupSessionClassification.MISSING_MANIFEST)
    if is_link_or_reparse(manifest_path) or not manifest_path.is_file():
        return _invalid_session(name, BackupSessionClassification.INVALID_MANIFEST)
    try:
        manifest_bytes = _read_regular_file(manifest_path)
        manifest = parse_backup_session_manifest_bytes(manifest_bytes)
    except BackupSessionBindingError:
        return _invalid_session(name, BackupSessionClassification.BINDING_UNVERIFIABLE)
    except (OSError, ValueError):
        return _invalid_session(name, BackupSessionClassification.INVALID_MANIFEST)

    files, directories, unsafe = _actual_session_tree(session)
    expected_files = {SESSION_MANIFEST_FILENAME, *(item.backup for item in manifest.items)}
    expected_directories = _expected_directories(manifest.items)
    if unsafe or files != expected_files or directories != expected_directories:
        return _invalid_session(
            name,
            BackupSessionClassification.UNEXPECTED_ENTRY,
            state=manifest.state,
        )

    tree = hashlib.sha256()
    verified_items: list[BackupSessionItem] = []
    try:
        file_digests: dict[str, ArtifactDigest] = {}
        for relative in sorted(expected_files, key=_sort_key):
            path = session / Path(*PurePosixPath(relative).parts)
            if path_uses_link_or_reparse(path) or not path.is_file():
                raise OSError("unsafe managed backup file")
            file_digests[relative] = _hash_file_and_tree(tree, relative, path)
        for item in manifest.items:
            if file_digests[item.backup] != item.digest:
                raise OSError("backup digest mismatch")
            verified_items.append(item)
    except OSError:
        return _invalid_session(
            name,
            BackupSessionClassification.INVALID_MANIFEST,
            state=manifest.state,
        )

    classification = (
        BackupSessionClassification.INTERRUPTED
        if manifest.state is BackupSessionState.OPEN
        else BackupSessionClassification.MANAGED
    )
    return BackupInventorySession(
        name,
        classification,
        manifest.state,
        classification is BackupSessionClassification.MANAGED
        and manifest.state is BackupSessionState.COMPLETE,
        hashlib.sha256(manifest_bytes).hexdigest(),
        tree.hexdigest(),
        tuple(verified_items),
    )


def _inspect_legacy_session(session: Path) -> BackupInventorySession:
    name = session.name
    if is_link_or_reparse(session) or not session.is_dir():
        return _invalid_session(name, BackupSessionClassification.UNEXPECTED_ENTRY)
    files, _, unsafe = _actual_session_tree(session)
    if unsafe or any(not path.casefold().endswith(".bak") for path in files):
        return _invalid_session(name, BackupSessionClassification.UNEXPECTED_ENTRY)
    return _invalid_session(name, BackupSessionClassification.LEGACY_UNMANAGED)


def build_backup_inventory(package_root: Path, backup_root: Path) -> BackupInventory:
    """Inspect a backup root without changing any filesystem entry."""

    validate_backup_root(package_root, backup_root)
    sessions: list[BackupInventorySession] = []
    try:
        entries = sorted(os.scandir(backup_root), key=lambda entry: _sort_key(entry.name))
    except OSError as exc:
        raise BackupInventoryInputError("backup root cannot be inspected") from exc
    for entry in entries:
        if entry.name.startswith(MANAGED_SESSION_PREFIX):
            sessions.append(_inspect_managed_session(Path(entry.path)))
        elif entry.name.startswith(LEGACY_SESSION_PREFIX):
            sessions.append(_inspect_legacy_session(Path(entry.path)))
    return BackupInventory(tuple(sessions))


def parse_backup_inventory_bytes(content: bytes) -> BackupInventory:
    """Parse Backup Inventory schema v1 for protected report replacement."""

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Backup Inventory JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Backup Inventory root")
    summary = payload.get("summary")
    raw_sessions = payload.get("sessions")
    schema_version = payload.get("schema_version")
    if not (
        payload.get("report_type") == "knowledge-importer-backup-inventory"
        and schema_version == 1
        and not isinstance(schema_version, bool)
        and isinstance(summary, dict)
        and isinstance(raw_sessions, list)
    ):
        raise ValueError("invalid Backup Inventory schema")
    count_keys = (
        "sessions",
        "managed",
        "orphaned",
        "legacy_unmanaged",
        "planning_eligible",
        "backup_files",
        "backup_bytes",
    )
    if not all(_is_nonnegative_int(summary.get(key)) for key in count_keys):
        raise ValueError("invalid Backup Inventory summary")
    sessions: list[BackupInventorySession] = []
    for value in raw_sessions:
        if not isinstance(value, dict):
            raise ValueError("invalid Backup Inventory session")
        session = value.get("session")
        classification = value.get("classification")
        state = value.get("state")
        eligible = value.get("planning_eligible")
        manifest_sha256 = value.get("session_manifest_sha256")
        tree_sha256 = value.get("tree_sha256")
        raw_items = value.get("items")
        if not (
            isinstance(session, str)
            and session
            and "\\" not in session
            and "/" not in session
            and not any(unicodedata.category(character) in {"Cc", "Cf"} for character in session)
            and classification in {candidate.value for candidate in BackupSessionClassification}
            and (state is None or state in {candidate.value for candidate in BackupSessionState})
            and isinstance(eligible, bool)
            and (manifest_sha256 is None or _is_sha256(manifest_sha256))
            and (tree_sha256 is None or _is_sha256(tree_sha256))
            and isinstance(raw_items, list)
        ):
            raise ValueError("invalid Backup Inventory session")
        items = tuple(_parse_session_item(item) for item in raw_items)
        parsed_classification = BackupSessionClassification(classification)
        parsed_state = BackupSessionState(state) if state is not None else None
        valid_eligibility = eligible is (
            parsed_classification is BackupSessionClassification.MANAGED
            and parsed_state is BackupSessionState.COMPLETE
        )
        valid_managed_metadata = (
            parsed_classification
            in {
                BackupSessionClassification.MANAGED,
                BackupSessionClassification.INTERRUPTED,
            }
        ) is (manifest_sha256 is not None and tree_sha256 is not None)
        valid_state = (
            parsed_classification is BackupSessionClassification.MANAGED
            and parsed_state
            in {
                BackupSessionState.COMPLETE,
                BackupSessionState.ROLLED_BACK,
                BackupSessionState.ROLLBACK_FAILED,
            }
            or parsed_classification is BackupSessionClassification.INTERRUPTED
            and parsed_state is BackupSessionState.OPEN
            or parsed_classification
            not in {
                BackupSessionClassification.MANAGED,
                BackupSessionClassification.INTERRUPTED,
            }
        )
        valid_items = (
            parsed_classification
            in {
                BackupSessionClassification.MANAGED,
                BackupSessionClassification.INTERRUPTED,
            }
            or not items
        )
        if (
            not valid_eligibility
            or not valid_managed_metadata
            or not valid_state
            or not valid_items
        ):
            raise ValueError("invalid Backup Inventory session semantics")
        if len({_comparison_key(item.source) for item in items}) != len(items) or len(
            {_comparison_key(item.backup) for item in items}
        ) != len(items):
            raise ValueError("duplicate Backup Inventory item")
        sessions.append(
            BackupInventorySession(
                session,
                parsed_classification,
                parsed_state,
                eligible,
                manifest_sha256,
                tree_sha256,
                items,
            )
        )
    inventory = BackupInventory(tuple(sessions))
    if inventory.payload()["summary"] != {key: summary[key] for key in count_keys}:
        raise ValueError("inconsistent Backup Inventory summary")
    if list(inventory.sessions) != sorted(
        inventory.sessions, key=lambda session: _sort_key(session.session)
    ):
        raise ValueError("non-deterministic Backup Inventory session order")
    if len({_comparison_key(session.session) for session in inventory.sessions}) != len(
        inventory.sessions
    ):
        raise ValueError("duplicate Backup Inventory session")
    return inventory


def is_backup_inventory_report(path: Path) -> bool:
    """Return whether an existing regular file is a valid Backup Inventory v1."""

    if is_link_or_reparse(path) or not path.is_file():
        return False
    try:
        parse_backup_inventory_bytes(path.read_bytes())
    except (OSError, ValueError):
        return False
    return True


def write_backup_inventory(path: Path, inventory: BackupInventory) -> None:
    """Write Backup Inventory v1 atomically."""

    write_json_atomically(path, inventory.payload())
