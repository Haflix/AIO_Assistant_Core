"""SafeUnpickler + Serializable mixin for B-066 hardening.

PR4 Stage K: post-auth pickle deserialization is constrained to a fixed
allowlist of safe stdlib types plus any plugin classes that opt in via
the Serializable mixin (or SerializableException for exception classes).

The pre-auth pickle.loads RCE surface (B-066) is closed by the mTLS
fingerprint pinning layer in plexus.networking — this module addresses
the post-auth defense-in-depth: a compromised pinned peer cannot RCE
other peers via crafted pickle payloads either.

Plugin authors:
- Custom data classes that traverse the wire: inherit `Serializable`.
- Custom exception classes raised in remote handlers: inherit
  `SerializableException`. Plain `Exception` subclasses raised inside
  remote handlers will be rewritten to `NetworkRequestException` on the
  receiver side because the receiver's SafeUnpickler does not recognize
  the unregistered class.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import io
import logging
import pickle
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple, Type

_logger = logging.getLogger(__name__)


# Module-level constant for the CLI command — referenced by migration errors
# elsewhere. If the CLI module name changes, update only this line.
FINGERPRINT_CLI_CMD: str = "python -m networking_cli show-fingerprint"


# --- Stdlib safe types ---

_BUILTIN_ALLOWLIST: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("builtins", "dict"),
        ("builtins", "list"),
        ("builtins", "str"),
        ("builtins", "int"),
        ("builtins", "float"),
        ("builtins", "bool"),
        ("builtins", "NoneType"),
        ("builtins", "tuple"),
        ("builtins", "set"),
        ("builtins", "frozenset"),
        ("builtins", "bytes"),
        ("builtins", "bytearray"),
        ("builtins", "complex"),
        ("builtins", "range"),
        ("builtins", "slice"),
        ("builtins", "object"),
        # C-161: array.array missing from the allowlist previously —
        # a plugin payload containing `array.array("i", [1,2,3])` would
        # be rejected by SafeUnpickler with an opaque "Disallowed
        # class" message on the receiver side. The reconstructor
        # callable that pickle dispatches through is also allowed.
        ("array", "array"),
        ("array", "_array_reconstructor"),
        ("datetime", "datetime"),
        ("datetime", "date"),
        ("datetime", "time"),
        ("datetime", "timedelta"),
        ("datetime", "timezone"),
        ("decimal", "Decimal"),
        ("pathlib", "PurePath"),
        ("pathlib", "PurePosixPath"),
        ("pathlib", "PureWindowsPath"),
        ("pathlib", "Path"),
        ("pathlib", "PosixPath"),
        ("pathlib", "WindowsPath"),
        ("uuid", "UUID"),
        ("collections", "OrderedDict"),
        ("collections", "deque"),
        ("collections", "defaultdict"),
        ("collections", "Counter"),
        ("enum", "Enum"),
        ("enum", "IntEnum"),
        ("enum", "Flag"),
        ("enum", "IntFlag"),
    }
)


# --- Project exceptions (static enumerate; mirrors plexus.exceptions) ---

_PROJECT_EXCEPTIONS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("plexus.exceptions", "RequestException"),
        ("plexus.exceptions", "NetworkRequestException"),
        ("plexus.exceptions", "NoLocalSubException"),
        ("plexus.exceptions", "ConfigException"),
        ("plexus.exceptions", "NodeException"),
        ("plexus.exceptions", "PluginTypeMismatchError"),
        ("plexus.exceptions", "PluginDependencyError"),
    }
)


# --- Project framework types — allowed across wire post-auth ---
# Stage N (PR4) — B-067 fix. plexus.utils.Event is the wire envelope
# for the first chunk of a request_event_stream response (LOCKED I).
# Without this allowlist entry, the receiver's SafeUnpickler rejects
# the Event pickled by the server and every cross-node
# request_event_stream call fails with
# "Disallowed class during deserialization: plexus.utils.Event".
# Static frozenset (not the dynamic Serializable opt-in) because Event
# is part of the framework, not a plugin-defined type.
_PROJECT_TYPES: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("plexus.utils", "Event"),
    }
)


# --- Static exception registry (pre-populated at module import) ---

_TRUSTED_EXCEPTION_MODULES: FrozenSet[str] = frozenset(
    {
        # Python 3.11+ aliased asyncio.TimeoutError -> builtins.TimeoutError
        # at the pickle stream layer (see PEP 678 / bpo-45390). Older
        # versions still pickle it under "asyncio". Keep both module names
        # so cross-node propagation works regardless of Python version.
        "builtins",
        "plexus.exceptions",
        "asyncio",
        "asyncio.exceptions",
        "concurrent.futures",
        "concurrent.futures._base",
        "pickle",
        "ssl",
        "socket",
        "json",
    }
)

_EXCEPTION_REGISTRY: Dict[Tuple[str, str], Type] = {}


# R3-SS-1 fix: process-terminating exception classes must never enter the
# registry. A compromised pinned peer could otherwise pickle SystemExit(0)
# (or KeyboardInterrupt / GeneratorExit) as an MSG_ERROR payload, and
# execute_remote / execute_remote_stream would raise it verbatim — SystemExit
# would terminate the receiving process with no stack trace. These classes
# are deliberately excluded from cross-node propagation; only ordinary
# Exception subclasses are allowed to round-trip.
_PROCESS_TERMINATING_EXCEPTIONS: FrozenSet[Type[BaseException]] = frozenset(
    {
        SystemExit,
        KeyboardInterrupt,
        GeneratorExit,
    }
)


def _populate_exception_registry():
    """Walk trusted exception modules at import time, register every
    BaseException subclass found. After this, find_class can do an O(1)
    membership check with zero import side effects on hot path.

    C-046 fix: module import failures are logged at WARNING with the
    module name and exception. Previously the failure was silently
    swallowed, leaving the registry partial — cross-network
    ``MSG_ERROR`` frames carrying exception types from a missing module
    would then be rejected with an opaque "not allowlisted" error and
    no diagnostic linking the failure back to the missing module.

    R3-SS-1 fix: process-terminating exception classes (SystemExit,
    KeyboardInterrupt, GeneratorExit) are explicitly excluded so a
    compromised peer cannot send one as MSG_ERROR and terminate the
    receiving process.
    """
    for module_name in _TRUSTED_EXCEPTION_MODULES:
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            _logger.warning(
                "_populate_exception_registry: trusted module %r failed to "
                "import — exception types from this module will not "
                "round-trip across the network: %s",
                module_name,
                exc,
            )
            continue
        for attr_name in dir(mod):
            try:
                obj = getattr(mod, attr_name)
            except Exception:
                continue
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseException)
                and obj not in _PROCESS_TERMINATING_EXCEPTIONS
            ):
                _EXCEPTION_REGISTRY[(module_name, attr_name)] = obj


_populate_exception_registry()


# --- Plugin custom-class registry ---

SERIALIZABLE_REGISTRY: Dict[Tuple[str, str], Type] = {}


class Serializable:
    """Plugin opt-in marker. Subclassing this auto-registers the class
    for safe deserialization across the network boundary.

    Example:
        from plexus.serialization import Serializable

        class MyPluginData(Serializable):
            ...

    Constraint: the receiving node must have the same class importable
    under the same (module, qualname) key. If a plugin uses different
    module paths on different nodes (e.g., absolute vs relative imports),
    deserialization will fail and fall back to a generic error.

    C-159: collision policy is WARN + last-wins. Two plugins both
    defining ``Response`` at module top level, or a hot-reload that
    re-imports + re-defines a class, register under the same
    ``(module, qualname)`` key. The second registration overwrites
    the first (last-wins matches hot-reload semantics — operators
    expect the freshly-loaded code to be the live one) and a WARNING
    is logged so genuine collisions between unrelated classes
    surface in the operator log instead of silently shadowing.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        key = (cls.__module__, cls.__qualname__)
        prev = SERIALIZABLE_REGISTRY.get(key)
        if prev is not None and prev is not cls:
            # Use the stdlib logger (not Plexus._logger) — this fires
            # at import time before any framework instance exists.
            logging.getLogger("plexus.serialization").warning(
                "[REGISTRY] Serializable collision at key %s: "
                "previous=%s.%s replaced by new=%s.%s. Last-wins; "
                "expected for hot-reload, unexpected for unrelated "
                "classes sharing a name at the same module path.",
                key,
                prev.__module__, prev.__qualname__,
                cls.__module__, cls.__qualname__,
            )
        SERIALIZABLE_REGISTRY[key] = cls


class SerializableException(Exception, Serializable):
    """Plugin-defined exceptions that need to traverse the network must
    inherit from this mixin. Plain Exception subclasses raised inside
    remote handlers will be replaced with NetworkRequestException on the
    receive side (see plexus.networking MSG_ERROR receive path).

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
        if key in _PROJECT_TYPES:
            return super().find_class(module, name)
        if key in _PROJECT_EXCEPTIONS:
            return super().find_class(module, name)
        if key in SERIALIZABLE_REGISTRY:
            return SERIALIZABLE_REGISTRY[key]
        if key in _EXCEPTION_REGISTRY:
            return _EXCEPTION_REGISTRY[key]
        raise pickle.UnpicklingError(
            f"Disallowed class during deserialization: {module}.{name}. "
            f"For plugin-defined types, inherit from plexus.serialization.Serializable. "
            f"For plugin-defined exceptions, inherit from "
            f"plexus.serialization.SerializableException."
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
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=365 * 100)
        )
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

    # W4-N2: Python 3.12+ rejects multi-dot suffixes in `with_suffix`; use
    # safe name concatenation for identical filesystem semantics.
    cert_tmp = cert_path.parent / (cert_path.name + ".tmp")
    key_tmp = key_path.parent / (key_path.name + ".tmp")
    cert_tmp.write_text(cert_pem, encoding="utf-8")
    # W2-E5: open + write + close with 0o600 mode set AT CREATION (POSIX).
    # ``Path.write_bytes`` + chmod was a TOCTOU window (file gets process
    # umask first, narrowed only after). On POSIX, no world-readable
    # window. On Windows the POSIX mode bits are ignored entirely by
    # `os.open`; access is gated by NTFS ACLs (Windows operators relying
    # on per-user filesystem ACLs are unaffected by the prior bug). The
    # `try`/`except (OSError, NotImplementedError)` on the chmod was the
    # original Windows-silent-failure path; this is removed by using
    # os.open which has no chmod step to swallow.
    key_fd = os.open(key_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(key_fd, key_pem)
    finally:
        os.close(key_fd)
    # C-169: rollback the cert rename on key-rename failure. The pair
    # is not atomic at the filesystem level (no transactional rename
    # of two files on the same dir), so a crash between the two
    # commits used to leave cert.pem updated and key.pem stale —
    # subsequent boot would load the new cert + old key and fail TLS
    # handshake with an opaque error. We capture the prior cert (if
    # any) before the rename so we can restore on failure.
    prior_cert: Optional[bytes] = None
    if cert_path.exists():
        try:
            prior_cert = cert_path.read_bytes()
        except OSError:
            prior_cert = None
    cert_tmp.replace(cert_path)
    try:
        key_tmp.replace(key_path)
    except Exception:
        # Roll back the cert: best-effort restore of the previous
        # cert content (if we captured it) so the boot can still
        # load a valid pair. If we had no prior cert (fresh
        # cluster), delete the new cert so the next boot regenerates
        # cleanly. The key_tmp file is left on disk for the operator
        # to inspect.
        if prior_cert is not None:
            try:
                cert_path.write_bytes(prior_cert)
            except OSError:
                pass
        else:
            try:
                cert_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    spki = cert.public_key().public_bytes(
        encoding=_ser.Encoding.DER,
        format=_ser.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = f"sha256:{hashlib.sha256(spki).hexdigest()}"
    return cert_path, key_path, fingerprint, cert_pem
