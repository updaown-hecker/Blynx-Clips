"""Headless credential storage for a private, persistent service volume."""
import hashlib
import os
import tempfile
from pathlib import Path


def credential_path(service: str, account: str) -> Path | None:
    directory = os.getenv("BLYNX_CREDENTIAL_DIR")
    if not directory:
        return None
    key = hashlib.sha256(f"{service}\0{account}".encode()).hexdigest()
    return Path(directory) / key


def read_credential(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise PermissionError("Credential file must be private")
    return path.read_text(encoding="utf-8")


def write_credential(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise PermissionError("Credential directory must be private")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        staged = Path(file.name)
        try:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
