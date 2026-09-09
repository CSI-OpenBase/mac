"""Scoped deletion for data owned by the local archive application."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .local_config import LocalSettings
from .local_store import (
    CLEAR_DATA_SCOPES,
    CLEAR_OPERATION_ID_RE,
    VIDEO_ID_RE,
    LocalStore,
)


CLEAR_DATA_LABELS = {
    "exports": "平台导出的原始数据",
    "comments": "用户评论数据",
    "all": "全部数据",
}
_TRASH_DIRECTORY = ".openbase-trash"
_TRASH_MARKER = ".csi-openbase-owned"
_STAGE_PREFIX = "clear-"
_OPERATION_SCHEMA = "csi-openbase.local-clear-operation"


class UnsafeCleanupPathError(RuntimeError):
    """Raised when an owned path cannot be deleted without following a link."""


@dataclass(frozen=True, slots=True)
class ClearDataResult:
    scope: str
    files_deleted: int
    directories_deleted: int
    pending_directories: int = 0


@dataclass(frozen=True, slots=True)
class _StagedOperation:
    operation_id: str
    scope: str
    manifest_path: Path
    stage_root: Path
    mappings: tuple[tuple[Path, Path], ...]


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_unsafe_boundary(path: Path) -> bool:
    """Return true for filesystem boundaries recursive deletion must not cross."""

    is_junction = getattr(path, "is_junction", None)
    try:
        return (
            path.is_symlink()
            or bool(is_junction and is_junction())
            or path.is_mount()
        )
    except OSError:
        return True


def _validate_location(path: Path, root: Path) -> None:
    absolute_root = Path(os.path.abspath(root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise UnsafeCleanupPathError(
            f"数据路径不在允许的工作目录中：{absolute_path}"
        ) from exc
    if not relative.parts:
        raise UnsafeCleanupPathError("不能删除工作目录本身")

    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise UnsafeCleanupPathError(
            f"数据路径指向工作目录之外：{absolute_path}"
        )

    current = absolute_root
    for part in relative.parts:
        current /= part
        if _exists(current) and _is_unsafe_boundary(current):
            raise UnsafeCleanupPathError(
                f"数据路径包含链接、目录联接点或挂载点，未执行清空：{current}"
            )


def _inspect_directory(path: Path, root: Path) -> tuple[int, int]:
    _validate_location(path, root)
    if not _exists(path):
        return 0, 0
    if not path.is_dir():
        raise UnsafeCleanupPathError(f"数据目录类型异常，未执行清空：{path}")

    files = 0
    directories = 1
    for current, child_directories, child_files in os.walk(path, followlinks=False):
        current_path = Path(current)
        for name in child_directories:
            child = current_path / name
            if _is_unsafe_boundary(child):
                raise UnsafeCleanupPathError(
                    f"数据目录包含链接、目录联接点或挂载点，未执行清空：{child}"
                )
            directories += 1
        for name in child_files:
            child = current_path / name
            if _is_unsafe_boundary(child):
                raise UnsafeCleanupPathError(
                    f"数据目录包含链接或挂载点，未执行清空：{child}"
                )
            files += 1
    return files, directories


def _rename_directory(source: Path, destination: Path) -> None:
    source.rename(destination)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return


def _remove_empty_trash_root(path: Path) -> None:
    marker = path / _TRASH_MARKER
    try:
        if any(child.name != _TRASH_MARKER for child in path.iterdir()):
            return
        marker.unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        return


def _operation_id_from_stage_name(name: str) -> str | None:
    operation_id = name.removeprefix(_STAGE_PREFIX)
    if name.startswith(_STAGE_PREFIX) and CLEAR_OPERATION_ID_RE.fullmatch(
        operation_id
    ):
        return operation_id
    return None


def _prepare_trash_root(root: Path) -> Path:
    trash_root = root / _TRASH_DIRECTORY
    _validate_location(trash_root, root)
    if _exists(trash_root):
        if not trash_root.is_dir():
            raise UnsafeCleanupPathError(
                f"待清理目录类型异常，未执行清空：{trash_root}"
            )
        marker = trash_root / _TRASH_MARKER
        if not _exists(marker):
            try:
                next(trash_root.iterdir())
            except StopIteration:
                marker.write_text(
                    "CSI OpenBase pending data cleanup\n", encoding="utf-8"
                )
            else:
                raise UnsafeCleanupPathError(
                    f"待清理目录缺少有效的程序标记，未执行清空：{trash_root}"
                )
    else:
        trash_root.mkdir()
        try:
            (trash_root / _TRASH_MARKER).write_text(
                "CSI OpenBase pending data cleanup\n", encoding="utf-8"
            )
        except Exception:
            _remove_empty_directory(trash_root)
            raise
    marker = trash_root / _TRASH_MARKER
    if not marker.is_file() or _is_unsafe_boundary(marker):
        raise UnsafeCleanupPathError(
            f"待清理目录缺少有效的程序标记，未执行清空：{trash_root}"
        )
    return trash_root


def _target_allowed(scope: str, relative: PurePosixPath) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return False
    if scope == "exports":
        return relative == PurePosixPath("exports")
    if scope == "all":
        return relative in {
            PurePosixPath("exports"),
            PurePosixPath("works"),
        }
    return (
        scope == "comments"
        and len(relative.parts) == 5
        and relative.parts[:3] == ("works", "videos", "douyin")
        and bool(VIDEO_ID_RE.fullmatch(relative.parts[3]))
        and relative.parts[4] == "comments"
    )


def _write_operation_manifest(
    trash_root: Path,
    root: Path,
    *,
    operation_id: str,
    scope: str,
    paths: list[Path],
) -> _StagedOperation:
    stage_name = f"{_STAGE_PREFIX}{operation_id}"
    stage_root = trash_root / stage_name
    manifest_path = trash_root / f"{stage_name}.json"
    absolute_root = Path(os.path.abspath(root))
    targets: list[dict[str, str]] = []
    mappings: list[tuple[Path, Path]] = []
    for index, path in enumerate(paths):
        relative = Path(os.path.abspath(path)).relative_to(absolute_root).as_posix()
        staged_name = f"{index:04d}-{path.name}"
        targets.append({"original": relative, "staged": staged_name})
        mappings.append((path, stage_root / staged_name))

    payload = {
        "schema": _OPERATION_SCHEMA,
        "version": 1,
        "operation_id": operation_id,
        "scope": scope,
        "targets": targets,
    }
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        manifest_path.unlink(missing_ok=True)
        raise
    return _StagedOperation(
        operation_id=operation_id,
        scope=scope,
        manifest_path=manifest_path,
        stage_root=stage_root,
        mappings=tuple(mappings),
    )


def _load_operation_manifest(manifest_path: Path, root: Path) -> _StagedOperation:
    if _is_unsafe_boundary(manifest_path) or not manifest_path.is_file():
        raise UnsafeCleanupPathError(
            f"待清理操作清单类型异常：{manifest_path}"
        )
    stage_name = manifest_path.name.removesuffix(".json")
    operation_id = _operation_id_from_stage_name(stage_name)
    if operation_id is None:
        raise UnsafeCleanupPathError(
            f"待清理操作清单名称异常：{manifest_path}"
        )
    try:
        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnsafeCleanupPathError(
            f"无法读取待清理操作清单：{manifest_path}"
        ) from exc
    if not isinstance(raw, dict):
        raise UnsafeCleanupPathError(f"待清理操作清单格式异常：{manifest_path}")

    scope = str(raw.get("scope") or "")
    targets = raw.get("targets")
    if (
        raw.get("schema") != _OPERATION_SCHEMA
        or raw.get("version") != 1
        or raw.get("operation_id") != operation_id
        or scope not in CLEAR_DATA_SCOPES
        or not isinstance(targets, list)
        or not targets
    ):
        raise UnsafeCleanupPathError(f"待清理操作清单格式异常：{manifest_path}")

    stage_root = manifest_path.parent / stage_name
    mappings: list[tuple[Path, Path]] = []
    seen_originals: set[PurePosixPath] = set()
    seen_staged: set[str] = set()
    for index, entry in enumerate(targets):
        if not isinstance(entry, dict):
            raise UnsafeCleanupPathError(
                f"待清理操作清单目标异常：{manifest_path}"
            )
        original_value = entry.get("original")
        staged_value = entry.get("staged")
        if not isinstance(original_value, str) or not isinstance(staged_value, str):
            raise UnsafeCleanupPathError(
                f"待清理操作清单目标异常：{manifest_path}"
            )
        relative = PurePosixPath(original_value)
        expected_staged = f"{index:04d}-{relative.name}"
        if (
            not _target_allowed(scope, relative)
            or staged_value != expected_staged
            or relative in seen_originals
            or staged_value in seen_staged
        ):
            raise UnsafeCleanupPathError(
                f"待清理操作清单目标越界或重复：{manifest_path}"
            )
        original = root.joinpath(*relative.parts)
        staged_path = stage_root / staged_value
        _validate_location(original, root)
        _validate_location(staged_path, root)
        seen_originals.add(relative)
        seen_staged.add(staged_value)
        mappings.append((original, staged_path))

    if _exists(stage_root):
        _inspect_directory(stage_root, root)
        unexpected = {
            child.name for child in stage_root.iterdir()
        } - seen_staged
        if unexpected:
            raise UnsafeCleanupPathError(
                f"待清理目录包含清单外内容：{stage_root}"
            )
    return _StagedOperation(
        operation_id=operation_id,
        scope=scope,
        manifest_path=manifest_path,
        stage_root=stage_root,
        mappings=tuple(mappings),
    )


def _list_staged_operations(root: Path) -> tuple[Path | None, list[_StagedOperation]]:
    trash_root = root / _TRASH_DIRECTORY
    if not _exists(trash_root):
        return None, []
    _prepare_trash_root(root)

    manifest_paths: dict[str, Path] = {}
    staged_ids: set[str] = set()
    for child in trash_root.iterdir():
        if child.name == _TRASH_MARKER:
            continue
        if child.suffix == ".json":
            operation_id = _operation_id_from_stage_name(child.stem)
            if operation_id is not None:
                manifest_paths[operation_id] = child
                continue
        operation_id = _operation_id_from_stage_name(child.name)
        if operation_id is not None and child.is_dir() and not _is_unsafe_boundary(child):
            staged_ids.add(operation_id)
            continue
        raise UnsafeCleanupPathError(
            f"待清理目录包含无法识别的内容，未执行清空：{child}"
        )
    missing_manifests = staged_ids - manifest_paths.keys()
    if missing_manifests:
        raise UnsafeCleanupPathError("待清理目录缺少操作清单，未执行清空")
    operations = [
        _load_operation_manifest(manifest_paths[key], root)
        for key in sorted(manifest_paths)
    ]
    return trash_root, operations


def _is_recreated_skeleton(path: Path, root: Path) -> bool:
    _validate_location(path, root)
    if not path.is_dir() or _is_unsafe_boundary(path):
        return False
    relative = Path(os.path.abspath(path)).relative_to(
        Path(os.path.abspath(root))
    ).as_posix()
    children = list(path.iterdir())
    if relative in {"exports"} or relative.endswith("/comments"):
        return not children
    if relative != "works":
        return False
    allowed = {"discovery", "videos"}
    if any(child.name not in allowed for child in children):
        return False
    return all(
        child.is_dir()
        and not _is_unsafe_boundary(child)
        and not any(child.iterdir())
        for child in children
    )


def _validate_prepared_recovery(operation: _StagedOperation, root: Path) -> None:
    for original, staged_path in operation.mappings:
        original_exists = _exists(original)
        staged_exists = _exists(staged_path)
        if staged_exists:
            _inspect_directory(staged_path, root)
            if original_exists and not _is_recreated_skeleton(original, root):
                raise UnsafeCleanupPathError(
                    f"待恢复数据与现有目录冲突，请勿继续写入：{original}"
                )
        elif not original_exists:
            raise UnsafeCleanupPathError(
                f"待恢复数据的原目录和暂存目录均不存在：{original}"
            )


def _restore_prepared_operation(
    operation: _StagedOperation, root: Path
) -> None:
    _validate_prepared_recovery(operation, root)
    restored: list[tuple[Path, Path]] = []
    try:
        for original, staged_path in operation.mappings:
            if not _exists(staged_path):
                continue
            if _exists(original):
                shutil.rmtree(original)
            _rename_directory(staged_path, original)
            restored.append((original, staged_path))
    except Exception as exc:
        try:
            for original, staged_path in reversed(restored):
                if _exists(original):
                    _rename_directory(original, staged_path)
        except Exception as rollback_exc:
            raise RuntimeError(
                "未提交的清理操作恢复失败；请勿继续写入该工作目录"
            ) from rollback_exc
        raise RuntimeError("未提交的清理操作恢复失败") from exc
    if _exists(operation.stage_root):
        operation.stage_root.rmdir()
    operation.manifest_path.unlink(missing_ok=True)


def _recover_pending_locked(settings: LocalSettings, store: LocalStore) -> int:
    trash_root, operations = _list_staged_operations(settings.data_home)
    committed = store.committed_clear_operations()
    operation_ids = {operation.operation_id for operation in operations}

    for operation in operations:
        committed_scope = committed.get(operation.operation_id)
        if committed_scope is not None and committed_scope != operation.scope:
            raise UnsafeCleanupPathError(
                "待清理操作清单与数据库记录不一致，未执行恢复"
            )
        if committed_scope is None:
            _validate_prepared_recovery(operation, settings.data_home)

    pending = 0
    for operation in operations:
        if operation.operation_id not in committed:
            _restore_prepared_operation(operation, settings.data_home)
            continue
        try:
            if _exists(operation.stage_root):
                shutil.rmtree(operation.stage_root)
            operation.manifest_path.unlink(missing_ok=True)
        except OSError:
            pending += 1
            continue
        store.finish_clear_operation(operation.operation_id)

    for operation_id in committed.keys() - operation_ids:
        store.finish_clear_operation(operation_id)
    if trash_root is not None:
        _remove_empty_trash_root(trash_root)
    return pending


def _rollback_staged(
    operation: _StagedOperation | None,
    created_roots: list[Path],
) -> None:
    try:
        for path in reversed(created_roots):
            if _exists(path):
                shutil.rmtree(path)
        if operation is not None:
            for original, staged_path in reversed(operation.mappings):
                if _exists(staged_path):
                    _rename_directory(staged_path, original)
            if _exists(operation.stage_root):
                operation.stage_root.rmdir()
            operation.manifest_path.unlink(missing_ok=True)
            _remove_empty_trash_root(operation.manifest_path.parent)
    except Exception as exc:
        raise RuntimeError(
            "清理准备失败，且未能完整恢复原数据目录；请勿继续写入该工作目录"
        ) from exc


def _stage_directories(
    paths: list[Path],
    root: Path,
    *,
    operation_id: str,
    scope: str,
) -> tuple[_StagedOperation | None, int, int]:
    inspected = [(path, *_inspect_directory(path, root)) for path in paths]
    files_deleted = sum(files for _, files, _ in inspected)
    directories_deleted = sum(directories for _, _, directories in inspected)
    existing = [path for path, _, _ in inspected if _exists(path)]
    if not existing:
        return None, files_deleted, directories_deleted

    trash_root = _prepare_trash_root(root)
    operation = _write_operation_manifest(
        trash_root,
        root,
        operation_id=operation_id,
        scope=scope,
        paths=existing,
    )
    try:
        operation.stage_root.mkdir()
        for original, staged_path in operation.mappings:
            _rename_directory(original, staged_path)
    except Exception:
        _rollback_staged(operation, [])
        raise
    return operation, files_deleted, directories_deleted


def _reject_authorization_overlap(
    paths: list[Path], settings: LocalSettings
) -> None:
    authorization = settings.browser_profile_dir.resolve(strict=False)
    for path in paths:
        target = path.resolve(strict=False)
        if (
            authorization == target
            or authorization.is_relative_to(target)
            or target.is_relative_to(authorization)
        ):
            raise UnsafeCleanupPathError(
                "浏览器登录授权目录位于所选清理范围内，未执行清空；"
                "请将 CSI_OPENBASE_SESSION_HOME 配置到工作数据目录之外"
            )


def _comment_directories(settings: LocalSettings) -> list[Path]:
    platform_root = settings.videos_dir / "douyin"
    _validate_location(platform_root, settings.data_home)
    if not _exists(platform_root):
        return []
    if not platform_root.is_dir():
        raise UnsafeCleanupPathError(
            f"视频档案目录类型异常，未执行清空：{platform_root}"
        )

    targets: list[Path] = []
    for child in platform_root.iterdir():
        if not VIDEO_ID_RE.fullmatch(child.name):
            continue
        if _is_unsafe_boundary(child):
            raise UnsafeCleanupPathError(
                f"视频档案路径包含链接、目录联接点或挂载点，未执行清空：{child}"
            )
        if child.is_dir() and _exists(child / "comments"):
            targets.append(child / "comments")
    return targets


def clear_local_data(
    settings: LocalSettings, store: LocalStore, scope: str
) -> ClearDataResult:
    if scope not in CLEAR_DATA_SCOPES:
        raise ValueError("不支持的数据清理范围")

    operation_id = uuid4().hex
    with store.exclusive_maintenance():
        _reject_authorization_overlap(
            [settings.data_home / _TRASH_DIRECTORY], settings
        )
        pending_directories = _recover_pending_locked(settings, store)
        if scope == "exports":
            targets = [settings.exports_dir]
        elif scope == "comments":
            targets = _comment_directories(settings)
        else:
            targets = [settings.exports_dir, settings.works_dir]

        _reject_authorization_overlap(targets, settings)
        operation, files_deleted, directories_deleted = _stage_directories(
            targets,
            settings.data_home,
            operation_id=operation_id,
            scope=scope,
        )

        created_roots: list[Path] = []
        try:
            if scope in {"exports", "all"}:
                settings.exports_dir.mkdir(parents=True, exist_ok=False)
                created_roots.append(settings.exports_dir)
            if scope == "all":
                settings.works_dir.mkdir(parents=True, exist_ok=False)
                created_roots.append(settings.works_dir)
                settings.discovery_dir.mkdir()
                settings.videos_dir.mkdir()

            store.clear_records(scope, operation_id=operation_id)
        except Exception:
            _rollback_staged(operation, created_roots)
            raise

        if operation is None:
            store.finish_clear_operation(operation_id)
        else:
            try:
                shutil.rmtree(operation.stage_root)
                operation.manifest_path.unlink(missing_ok=True)
            except OSError:
                pending_directories += 1
            else:
                store.finish_clear_operation(operation_id)
                _remove_empty_trash_root(operation.manifest_path.parent)

    return ClearDataResult(
        scope=scope,
        files_deleted=files_deleted,
        directories_deleted=directories_deleted,
        pending_directories=pending_directories,
    )


def recover_local_cleanup(settings: LocalSettings, store: LocalStore) -> int:
    """Recover an interrupted cleanup before background jobs can start."""

    with store.exclusive_maintenance():
        _reject_authorization_overlap(
            [settings.data_home / _TRASH_DIRECTORY], settings
        )
        pending_directories = _recover_pending_locked(settings, store)
        settings.exports_dir.mkdir(parents=True, exist_ok=True)
        settings.discovery_dir.mkdir(parents=True, exist_ok=True)
        settings.videos_dir.mkdir(parents=True, exist_ok=True)
        return pending_directories
