"""Bounded catalog of existing Phase9 audit artifacts. Never reads .env."""
import json
import os
from pathlib import Path

MAX_FILE = 4*1024*1024


class FileCatalog:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def scan(self):
        scope = self.root/'artifacts/phase9'
        if not scope.is_dir() or scope.is_symlink() or (self.root/'artifacts').is_symlink():
            return []
        result = []
        visited = 0
        for folder, dirs, files in os.walk(scope, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not (Path(folder)/name).is_symlink())[:256]
            for name in sorted(files):
                visited += 1
                if visited > 2048 or len(result) == 256:
                    return result
                path = Path(folder)/name
                if path.suffix not in {'.json', '.log'} or path.is_symlink():
                    continue
                try:
                    if path.stat().st_size <= MAX_FILE or path.suffix == '.log':
                        result.append(path.relative_to(self.root).as_posix())
                except OSError:
                    continue
        return result

    def _path(self, ref):
        path = self.root/ref
        if ref not in self.scan() or not path.resolve().is_relative_to(self.root/'artifacts/phase9'):
            raise ValueError('INVALID_ARTIFACT_REFERENCE')
        for item in (path, *path.parents):
            if item == self.root:
                break
            if item.is_symlink():
                raise ValueError('INVALID_ARTIFACT_REFERENCE')
        return path

    def read_json(self, ref):
        path = self._path(ref)
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                raw = stream.read(MAX_FILE+1)
            if len(raw) > MAX_FILE:
                raise ValueError('ARTIFACT_SIZE_LIMIT')
            result = json.loads(raw, parse_constant=lambda _: None)
            if not isinstance(result, dict):
                raise ValueError('INVALID_ARTIFACT')
            return result
        except (OSError, json.JSONDecodeError, RecursionError):
            raise ValueError('UNREADABLE_ARTIFACT') from None

    def tail(self, ref, *, with_truncation=False):
        path = self._path(ref)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size-65536))
            raw = stream.read(65536)
        lines = raw.decode('utf-8', errors='replace').splitlines()
        truncated = size > 65536
        result = lines[1:] if truncated else lines
        return (result, truncated) if with_truncation else result
