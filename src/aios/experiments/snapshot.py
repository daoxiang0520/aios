from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class SnapshotIntegrityError(RuntimeError):
    pass


class ContentAddressedSnapshotStore:
    """Deduplicated immutable file objects plus deterministic tree manifests."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.objects = self.root / "objects" / "sha256"
        self.manifests = self.root / "manifests"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)

    def capture(self, source: Path) -> dict[str, Any]:
        source = source.resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Snapshot source is not a directory: {source}")
        files: dict[str, dict[str, Any]] = {}
        total_bytes = 0
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise SnapshotIntegrityError(f"Symlinks are not replayable: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            object_path = self.object_path(digest)
            if not object_path.exists():
                object_path.parent.mkdir(parents=True, exist_ok=True)
                object_path.write_bytes(payload)
            mode = path.stat().st_mode & 0o777
            files[relative] = {"sha256": digest, "size": len(payload), "mode": mode}
            total_bytes += len(payload)
        manifest_hash = self._manifest_hash(files)
        manifest = {
            "kind": "content_addressed_tree_v1", "manifest_hash": manifest_hash,
            "files": files, "file_count": len(files), "total_bytes": total_bytes,
        }
        manifest_path = self.manifests / f"{manifest_hash}.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )
        return {key: value for key, value in manifest.items() if key != "files"}

    def capture_paths(self, source: Path, relative_paths: list[str]) -> dict[str, Any]:
        """Capture an explicit, bounded file set while reusing the same object store."""
        source = source.resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Snapshot source is not a directory: {source}")
        files: dict[str, dict[str, Any]] = {}
        total_bytes = 0
        for relative in sorted(set(relative_paths)):
            relative_path = self._validate_relative(relative)
            path = (source / relative_path).resolve()
            if source not in path.parents or path.is_symlink() or not path.is_file():
                raise SnapshotIntegrityError(f"Snapshot source file is invalid: {relative}")
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            object_path = self.object_path(digest)
            if not object_path.exists():
                object_path.parent.mkdir(parents=True, exist_ok=True)
                object_path.write_bytes(payload)
            files[relative_path.as_posix()] = {
                "sha256": digest, "size": len(payload), "mode": path.stat().st_mode & 0o777,
            }
            total_bytes += len(payload)
        manifest_hash = self._manifest_hash(files)
        manifest = {
            "kind": "content_addressed_tree_v1", "manifest_hash": manifest_hash,
            "files": files, "file_count": len(files), "total_bytes": total_bytes,
        }
        manifest_path = self.manifests / f"{manifest_hash}.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )
        return {key: value for key, value in manifest.items() if key != "files"}

    def load(self, manifest_hash: str) -> dict[str, Any]:
        path = self.manifests / f"{self._validate_digest(manifest_hash)}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Unknown snapshot manifest: {manifest_hash}")
        return json.loads(path.read_text(encoding="utf-8"))

    def verify(self, manifest_hash: str) -> dict[str, Any]:
        try:
            manifest = self.load(manifest_hash)
            files = manifest.get("files")
            if not isinstance(files, dict) or self._manifest_hash(files) != manifest_hash:
                raise SnapshotIntegrityError("Snapshot manifest hash mismatch")
            for relative, entry in files.items():
                self._validate_relative(relative)
                digest = self._validate_digest(str(entry.get("sha256", "")))
                path = self.object_path(digest)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise SnapshotIntegrityError(f"Missing or corrupt object: {digest}")
            return {"valid": True, "manifest_hash": manifest_hash, "file_count": len(files)}
        except (OSError, ValueError, KeyError, json.JSONDecodeError, SnapshotIntegrityError) as exc:
            return {"valid": False, "manifest_hash": manifest_hash, "error": f"{type(exc).__name__}: {exc}"}

    def restore(self, manifest_hash: str, destination: Path) -> dict[str, Any]:
        check = self.verify(manifest_hash)
        if not check["valid"]:
            raise SnapshotIntegrityError(check["error"])
        destination = destination.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError(f"Restore destination must be empty: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        manifest = self.load(manifest_hash)
        for relative, entry in manifest["files"].items():
            relative_path = self._validate_relative(relative)
            target = (destination / relative_path).resolve()
            if destination not in target.parents:
                raise SnapshotIntegrityError(f"Snapshot path escapes destination: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.object_path(entry["sha256"]).read_bytes())
            try:
                os.chmod(target, int(entry.get("mode", 0o644)))
            except OSError:
                pass
        return {"path": str(destination), **check}

    def tree_hash(self, root: Path) -> str:
        root = root.resolve()
        files: dict[str, dict[str, Any]] = {}
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file() and not path.is_symlink():
                payload = path.read_bytes()
                files[path.relative_to(root).as_posix()] = {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload), "mode": path.stat().st_mode & 0o777,
                }
        return self._manifest_hash(files)

    def object_path(self, digest: str) -> Path:
        digest = self._validate_digest(digest)
        return self.objects / digest[:2] / digest[2:]

    @staticmethod
    def _manifest_hash(files: dict[str, Any]) -> str:
        encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_digest(value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("Invalid SHA256 digest")
        return value

    @staticmethod
    def _validate_relative(value: str) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value:
            raise SnapshotIntegrityError(f"Invalid snapshot path: {value}")
        return path
