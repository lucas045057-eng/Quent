from hashlib import sha256
from pathlib import Path


def normalized_sha256(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline=None) as migration:
        return sha256(migration.read().encode("utf-8")).hexdigest()
