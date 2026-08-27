from __future__ import annotations

import csv
import json
import mimetypes
import zipfile
from pathlib import Path
from typing import Any

from .config import PermissionConfig
from .sandbox import DockerSandboxBroker


class ResourceAdapter:
    """Structured observations behind the single model-visible `read` primitive."""

    DEFAULT_TEXT_CHARACTERS = 12_000
    DEFAULT_TABLE_ROWS = 5

    TEXT_SUFFIXES = {
        ".txt", ".md", ".py", ".json", ".yaml", ".yml", ".toml", ".html",
        ".css", ".js", ".ts", ".tsx", ".jsx", ".xml", ".sql", ".sh", ".ps1",
    }

    def __init__(self, permissions: PermissionConfig, sandbox: DockerSandboxBroker | None):
        self.permissions = permissions
        self.sandbox = sandbox

    def read(self, path: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        target = Path(path)
        if target.is_dir():
            return self._directory(target)
        size = target.stat().st_size
        if size > self.permissions.max_read_bytes:
            raise ValueError(f"File exceeds {self.permissions.max_read_bytes} bytes")
        suffix = target.suffix.casefold()
        if suffix == ".pdf":
            return self._container_resource(target, "pdf", offset, limit)
        if suffix == ".xlsx":
            return self._container_resource(target, "xlsx", offset, limit)
        if suffix == ".csv":
            return self._csv(target, offset, limit)
        if suffix == ".zip":
            return self._archive(target)
        if suffix in self.TEXT_SUFFIXES or self._looks_textual(target):
            return self._text(target, offset, limit)
        return self._resource(
            target,
            self._media_type(target),
            {"size": size},
            [],
            detail="No structured representation adapter is registered for this format.",
        )

    def legacy_list(self, path: str) -> list[dict[str, Any]]:
        value = self.read(path)
        representations = value["resource"].get("representations", [])
        if representations and representations[0].get("kind") == "directory_entries":
            return representations[0]["entries"]
        raise NotADirectoryError(path)

    def legacy_text(self, path: str) -> str:
        value = self.read(path)
        representations = value["resource"].get("representations", [])
        if representations and representations[0].get("kind") == "text":
            return str(representations[0].get("text", ""))
        return json.dumps(value, ensure_ascii=False, indent=2)

    def _directory(self, target: Path) -> dict[str, Any]:
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            entries.append({
                "name": child.name,
                "path": self._display_path(child),
                "type": "directory" if child.is_dir() else self._media_type(child),
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
                "modified_ns": child.stat().st_mtime_ns,
            })
        return self._resource(
            target, "directory", {"entries": len(entries)},
            [{"kind": "directory_entries", "entries": entries}],
        )

    def _text(self, target: Path, offset: int, limit: int | None) -> dict[str, Any]:
        text = target.read_text(encoding="utf-8", errors="replace")
        start = max(0, offset)
        cap = min(self.DEFAULT_TEXT_CHARACTERS, self.permissions.max_read_bytes) if limit is None else max(0, min(limit, self.permissions.max_read_bytes))
        selected = text[start:start + cap]
        return self._resource(
            target, self._media_type(target),
            {"size": target.stat().st_size, "characters": len(text)},
            [{"kind": "text", "offset": start, "text": selected, "truncated": start + len(selected) < len(text)}],
        )

    def _csv(self, target: Path, offset: int, limit: int | None) -> dict[str, Any]:
        with target.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.reader(handle, dialect))
        start = max(0, offset)
        row_limit = self.DEFAULT_TABLE_ROWS if limit is None else max(0, min(limit, 100))
        preview = rows[start:start + row_limit]
        width = max((len(row) for row in rows), default=0)
        return self._resource(
            target, "text/csv",
            {"size": target.stat().st_size, "rows": len(rows), "columns": width},
            [{"kind": "table_preview", "row_offset": start, "rows": preview, "truncated": start + len(preview) < len(rows)}],
        )

    def _archive(self, target: Path) -> dict[str, Any]:
        with zipfile.ZipFile(target) as archive:
            entries = [
                {"path": item.filename, "size": item.file_size, "compressed_size": item.compress_size, "is_dir": item.is_dir()}
                for item in archive.infolist()[:200]
            ]
            total = len(archive.infolist())
        return self._resource(
            target, "application/zip", {"size": target.stat().st_size, "entries": total},
            [{"kind": "archive_entries", "entries": entries, "truncated": total > len(entries)}],
        )

    def _container_resource(self, target: Path, kind: str, offset: int, limit: int | None) -> dict[str, Any]:
        if self.sandbox is None or self.sandbox.session is None:
            raise RuntimeError(f"{kind.upper()} resource reading requires an active Docker sandbox")
        try:
            relative = target.resolve().relative_to(self.sandbox.session.path.resolve()).as_posix()
        except ValueError as exc:
            raise RuntimeError("Resource path is outside the active sandbox workspace") from exc
        output_budget = min(self.permissions.max_read_bytes, self.DEFAULT_TEXT_CHARACTERS) if limit is None else self.permissions.max_read_bytes
        result = self.sandbox.read_resource(
            relative, kind=kind, offset=max(0, offset), limit=limit,
            max_output_bytes=output_budget,
        )
        if result.get("exit_code") != 0:
            raise RuntimeError(str(result.get("stderr") or result.get("stdout") or "Resource adapter failed"))
        try:
            value = json.loads(str(result.get("stdout", "")))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Resource adapter returned invalid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("resource"), dict):
            raise RuntimeError("Resource adapter returned an invalid observation")
        return value

    def _resource(
        self, target: Path, resource_type: str, metadata: dict[str, Any],
        representations: list[dict[str, Any]], *, detail: str | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "path": self._display_path(target),
            "type": resource_type,
            "metadata": metadata,
            "representations": representations,
        }
        if detail:
            value["detail"] = detail
        return {"resource": value}

    def _display_path(self, target: Path) -> str:
        if self.sandbox is not None and self.sandbox.session is not None:
            try:
                return target.resolve().relative_to(self.sandbox.session.path.resolve()).as_posix() or "."
            except ValueError:
                pass
        return target.name

    @staticmethod
    def _media_type(path: Path) -> str:
        if path.is_dir():
            return "directory"
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    @staticmethod
    def _looks_textual(path: Path) -> bool:
        with path.open("rb") as handle:
            sample = handle.read(4096)
        return b"\x00" not in sample
