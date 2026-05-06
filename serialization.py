"""SafeUnpickler + Serializable mixin for B-066 hardening.

PR4 Stage K: post-auth pickle deserialization is constrained to a fixed
allowlist of safe stdlib types plus any plugin classes that opt in via
the Serializable mixin (or SerializableException for exception classes).

The pre-auth pickle.loads RCE surface (B-066) is closed by the mTLS
fingerprint pinning layer in networking.py — this module addresses the
post-auth defense-in-depth: a compromised pinned peer cannot RCE other
peers via crafted pickle payloads either.

Plugin authors:
- Custom data classes that traverse the wire: inherit `Serializable`.
- Custom exception classes raised in remote handlers: inherit
  `SerializableException`. Plain `Exception` subclasses raised inside
  remote handlers will be rewritten to `NetworkRequestException` on the
  receiver side because the receiver's SafeUnpickler does not recognize
  the unregistered class.
"""

from __future__ import annotations

import collections
import datetime
import decimal
import enum
import hashlib
import importlib
import io
import pathlib
import pickle
import uuid
from pathlib import Path
from typing import Any, Dict, FrozenSet, Tuple, Type


# Module-level constant for the CLI command — referenced by migration errors
# elsewhere. If the CLI module name changes, update only this line.
FINGERPRINT_CLI_CMD: str = "python -m networking_cli show-fingerprint"


# --- Stdlib safe types ---

_BUILTIN_ALLOWLIST: FrozenSet[Tuple[str, str]] = frozenset({
    ("builtins", "dict"), ("builtins", "list"), ("builtins", "str"),
    ("builtins", "int"), ("builtins", "float"), ("builtins", "bool"),
    ("builtins", "NoneType"), ("builtins", "tuple"), ("builtins", "set"),
    ("builtins", "frozenset"), ("builtins", "bytes"), ("builtins", "bytearray"),
    ("builtins", "complex"), ("builtins", "range"), ("builtins", "slice"),
    ("builtins", "object"),
    ("datetime", "datetime"), ("datetime", "date"), ("datetime", "time"),
    ("datetime", "timedelta"), ("datetime", "timezone"),
    ("decimal", "Decimal"),
    ("pathlib", "PurePath"), ("pathlib", "PurePosixPath"),
    ("pathlib", "PureWindowsPath"), ("pathlib", "Path"),
    ("pathlib", "PosixPath"), ("pathlib", "WindowsPath"),
    ("uuid", "UUID"),
    ("collections", "OrderedDict"), ("collections", "deque"),
    ("collections", "defaultdict"), ("collections", "Counter"),
    ("enum", "Enum"), ("enum", "IntEnum"), ("enum", "Flag"), ("enum", "IntFlag"),
})


# --- Project exceptions (static enumerate; avoids editing protected exceptions.py) ---

_PROJECT_EXCEPTIONS: FrozenSet[Tuple[str, str]] = frozenset({
    ("exceptions", "RequestException"),
    ("exceptions", "NetworkRequestException"),
    ("exceptions", "NoLocalSubException"),
    ("exceptions", "ConfigException"),
    ("exceptions", "NodeException"),
    ("exceptions", "PluginTypeMissmatchError"),
})


# --- Static exception registry (pre-populated at module import) ---

_TRUSTED_EXCEPTION_MODULES: FrozenSet[str] = frozenset({
    # Python 3.11+ aliased asyncio.TimeoutError -> builtins.TimeoutError
    # at the pickle stream layer (see PEP 678 / bpo-45390). Older
    # versions still pickle it under "asyncio". Keep both module names
    # so cross-node propagation works regardless of Python version.
    "builtins", "exceptions", "asyncio", "asyncio.exceptions",
    "concurrent.futures", "concurrent.futures._base",
    "pickle", "ssl", "socket", "json",
})

_EXCEPTION_REGISTRY: Dict[Tuple[str, str], Type] = {}


def _populate_exception_registry():
    """Walk trusted exception modules at import time, register every
    BaseException subclass found. After this, find_class can do an O(1)
    membership check with zero import side effects on hot path.
    """
    for module_name in _TRUSTED_EXCEPTION_MODULES:
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            continue
        for attr_name in dir(mod):
            try:
                obj = getattr(mod, attr_name)
            except Exception:
                continue
            if isinstance(obj, type) and issubclass(obj, BaseException):
                _EXCEPTION_REGISTRY[(module_name, attr_name)] = obj


_populate_exception_registry()


# --- Plugin custom-class registry ---

SERIALIZABLE_REGISTRY: Dict[Tuple[str, str], Type] = {}


class Serializable:
    """Plugin opt-in marker. Subclassing this auto-registers the class
    for safe deserialization across the network boundary.

    Example:
        from serialization import Serializable

        class MyPluginData(Serializable):
            ...

    Constraint: the receiving node must have the same class importable
    under the same (module, qualname) key. If a plugin uses different
    module paths on different nodes (e.g., absolute vs relative imports),
    deserialization will fail and fall back to a generic error.
    """
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        SERIALIZABLE_REGISTRY[(cls.__module__, cls.__qualname__)] = cls


class SerializableException(Exception, Serializable):
    """Plugin-defined exceptions that need to traverse the network must
    inherit from this mixin. Plain Exception subclasses raised inside
    remote handlers will be replaced with NetworkRequestException on the
    receive side (see networking.py MSG_ERROR receive path).

    For exception subclasses with complex __init__ signatures, ensure
    the args passed to super().__init__() are pickle-friendly (strings,
    primitives). Required-positional-args constructors may break pickle
    reconstruction; use `__init__(self, message, **kwargs)` pattern.
    """
    pass


# --- Unpickler ---

class SafeUnpickler(pickle.Unpickler):
    """Restricted unpickler. Only types in the allowlist, registered via
    Serializable / SerializableException, or in the pre-populated
    exception registry can be reconstructed.

    Critical security property: find_class never calls super().find_class
    BEFORE confirming membership. A malicious peer cannot trigger arbitrary
    module imports via crafted (module, name) tuples.
    """

    def find_class(self, module: str, name: str):
        key = (module, name)
        if key in _BUILTIN_ALLOWLIST:
            return super().find_class(module, name)
        if key in _PROJECT_EXCEPTIONS:
            return super().find_class(module, name)
        if key in SERIALIZABLE_REGISTRY:
            return SERIALIZABLE_REGISTRY[key]
        if key in _EXCEPTION_REGISTRY:
            return _EXCEPTION_REGISTRY[key]
        raise pickle.UnpicklingError(
            f"Disallowed class during deserialization: {module}.{name}. "
            f"For plugin-defined types, inherit from serialization.Serializable. "
            f"For plugin-defined exceptions, inherit from "
            f"serialization.SerializableException."
        )


def safe_loads(data: bytes) -> Any:
    """Drop-in replacement for pickle.loads using SafeUnpickler."""
    return SafeUnpickler(io.BytesIO(data)).load()


# --- Standalone keypair generator (used by smoke harness + identity bootstrap) ---

def generate_keypair(keys_dir: str, hostname: str) -> Tuple[Path, Path, str, str]:
    """Generate a self-signed cert + key pair, write to keys_dir, return
    (cert_path, key_path, fingerprint, cert_pem_text).

    Fingerprint is `sha256:<hex>` of the SubjectPublicKeyInfo DER.
    cert_pem_text is the PEM string suitable for pasting into another
    node's peers[].cert_pem field (or written to a file referenced by
    cert_file).

    Cert validity is 100 years. BasicConstraints(ca=True) is set as a
    critical extension because OpenSSL requires this when the cert is
    used as a trust anchor in CERT_REQUIRED mode (peer config feeds
    each peer's cert into load_verify_locations).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import os

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365 * 100))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(_ser.Encoding.PEM).decode()
    key_pem = private_key.private_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PrivateFormat.PKCS8,
        encryption_algorithm=_ser.NoEncryption(),
    )

    keys_path = Path(keys_dir)
    keys_path.mkdir(parents=True, exist_ok=True)
    cert_path = keys_path / "cert.pem"
    key_path = keys_path / "key.pem"

    cert_tmp = cert_path.with_suffix(".pem.tmp")
    key_tmp = key_path.with_suffix(".pem.tmp")
    cert_tmp.write_text(cert_pem, encoding="utf-8")
    key_tmp.write_bytes(key_pem)
    try:
        os.chmod(key_tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    cert_tmp.replace(cert_path)
    key_tmp.replace(key_path)

    spki = cert.public_key().public_bytes(
        encoding=_ser.Encoding.DER,
        format=_ser.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = f"sha256:{hashlib.sha256(spki).hexdigest()}"
    return cert_path, key_path, fingerprint, cert_pem
