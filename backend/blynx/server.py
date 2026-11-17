"""Headless credential storage for a private, persistent service volume."""
import hashlib
from ipaddress import IPv4Address, IPv4Network
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


PRIVATE_NETWORKS = tuple(IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def lan_ip() -> str:
    value = os.getenv("BLYNX_LAN_IP", "").strip()
    if not value:
        return ""
    try:
        address = IPv4Address(value)
        if any(address in network for network in PRIVATE_NETWORKS):
            return str(address)
    except ValueError:
        pass
    raise ValueError("BLYNX_LAN_IP must be a private IPv4 address assigned to the server")
