"""PR2 config migration tool — one-shot.

Walks a tree converting pre-PR2 config shapes to PR2 dict-form:

    plugin_config.yml :  endpoints: [ {access_name: x, ...}, ... ]
                      -> endpoints: { x: { ... } }
                         (drops the redundant access_name field; KEEPS the
                          internal_name field as-is for explicitness during
                          migration. After PR2, internal_name defaults to
                          the key when absent and may be hand-trimmed later.)

    config.yml /
    test_config.yml   :  - name: P
                           enabled: true
                           arguments: {...}
                      -> - name: P
                           enabled: true
                           overrides:
                             arguments: {...}

Uses ``ruamel.yaml`` to preserve comments, indentation, anchors. Excludes
``_old/``, ``.git/``, ``node_modules/``, ``__pycache__/``, ``_private/``
(walk that root separately with ``--root <_private_path>``).

Usage:

    python tools/migrate_pr2_config.py --root .
    python tools/migrate_pr2_config.py --root /path/to/_private --dry-run

After PR2 merges this script is deleted (single-use).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Tuple

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ruamel.yaml is required. Install with `pip install ruamel.yaml`.\n"
    )
    sys.exit(2)


EXCLUDED_DIR_NAMES = {
    "_old",
    ".git",
    "node_modules",
    "__pycache__",
    "_private",  # opt-in via explicit --root for the private repo side
    "worktrees",  # don't recurse into other worktrees from this one
    ".venv",
    "venv",
    "env",
}


def _make_yaml() -> YAML:
    """Configured ruamel YAML for round-trip with reasonable formatting."""
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    # Wide width prevents premature line-wrapping of long descriptions.
    y.width = 4096
    return y


def _iter_target_files(root: Path) -> List[Tuple[Path, str]]:
    """Walk `root`, yielding (path, kind) tuples where kind is one of
    ``plugin_config`` or ``main_config``.
    """
    out: List[Tuple[Path, str]] = []
    main_names = {"config.yml", "test_config.yml", "config.example.yml"}
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames to prune walk
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        for fname in filenames:
            if fname == "plugin_config.yml":
                out.append((Path(dirpath) / fname, "plugin_config"))
            elif fname in main_names and Path(dirpath) == root:
                # Only main configs at the root of the tree get migrated.
                out.append((Path(dirpath) / fname, "main_config"))
    return out


# ── plugin_config.yml conversion ─────────────────────────────────────────


def _convert_endpoints_list_to_dict(doc) -> Tuple[bool, str]:
    """If `doc['endpoints']` is a list, convert it to a dict keyed by each
    entry's `access_name`. Drops the redundant `access_name` field from each
    entry (it becomes the key). KEEPs the `internal_name` field for
    explicitness.

    Returns (changed, message).
    """
    if not isinstance(doc, CommentedMap) and not isinstance(doc, dict):
        return False, "not a mapping"
    if "endpoints" not in doc:
        return False, "no endpoints field"

    eps = doc.get("endpoints")
    if eps is None:
        # `endpoints:` with no value — convert null to empty dict for
        # uniformity. Many existing files have this shape.
        doc["endpoints"] = CommentedMap()
        return True, "endpoints: null -> {}"

    if isinstance(eps, dict):
        return False, "already dict-form"

    if isinstance(eps, list):
        if len(eps) == 0:
            doc["endpoints"] = CommentedMap()
            return True, "endpoints: [] -> {}"

        new_map = CommentedMap()
        for idx, entry in enumerate(eps):
            if not isinstance(entry, (dict, CommentedMap)):
                raise ValueError(
                    f"endpoints[{idx}] is not a mapping "
                    f"(got {type(entry).__name__}); cannot migrate"
                )
            access_name = entry.get("access_name")
            if not isinstance(access_name, str) or not access_name.strip():
                raise ValueError(
                    f"endpoints[{idx}] missing valid access_name; cannot migrate"
                )
            if access_name in new_map:
                raise ValueError(
                    f"endpoints[{idx}] duplicate access_name "
                    f"{access_name!r} in list-form; reject"
                )
            new_entry = CommentedMap()
            for k, v in entry.items():
                if k == "access_name":
                    continue
                new_entry[k] = v
            new_map[access_name] = new_entry
        doc["endpoints"] = new_map
        return True, f"endpoints: list({len(eps)}) -> dict({len(new_map)})"

    return False, f"endpoints unexpected type {type(eps).__name__}"


# ── main config (config.yml / test_config.yml) conversion ────────────────


def _wrap_arguments_into_overrides(plugin_entry: CommentedMap) -> Tuple[bool, str]:
    """For a single plugin_entry (a dict in the `plugins:` list), if it has
    a top-level ``arguments:`` field, wrap it under ``overrides:``.

    Returns (changed, message).
    """
    if "arguments" not in plugin_entry:
        return False, "no arguments field"

    args_value = plugin_entry["arguments"]

    # Build / extend the overrides block.
    if "overrides" in plugin_entry:
        ov = plugin_entry["overrides"]
        if not isinstance(ov, (dict, CommentedMap)):
            return False, f"overrides exists but is {type(ov).__name__}; skip"
        if "arguments" in ov:
            return False, "overrides.arguments already set; skip"
        ov["arguments"] = args_value
        del plugin_entry["arguments"]
        return True, "moved arguments -> overrides.arguments (existing overrides)"

    new_overrides = CommentedMap()
    new_overrides["arguments"] = args_value
    plugin_entry["overrides"] = new_overrides
    del plugin_entry["arguments"]
    return True, "wrapped arguments -> overrides.arguments"


def _migrate_main_config(doc) -> List[str]:
    """Walk doc['plugins'] and wrap entry-level `arguments:` into
    `overrides: { arguments: ... }`. Returns list of per-plugin change msgs.
    """
    msgs: List[str] = []
    if not isinstance(doc, (dict, CommentedMap)):
        return msgs
    plugins = doc.get("plugins")
    if not isinstance(plugins, (list, CommentedSeq)):
        return msgs
    for idx, entry in enumerate(plugins):
        if not isinstance(entry, (dict, CommentedMap)):
            continue
        name = entry.get("name", f"<idx={idx}>")
        changed, msg = _wrap_arguments_into_overrides(entry)
        if changed:
            msgs.append(f"  plugin {name!r}: {msg}")
    return msgs


# ── driver ───────────────────────────────────────────────────────────────


def migrate_file(path: Path, kind: str, *, dry_run: bool, yaml) -> List[str]:
    """Migrate a single file. Returns a list of per-file change messages."""
    msgs: List[str] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            doc = yaml.load(f)
    except Exception as e:
        return [f"{path}: ERROR loading: {e}"]

    if doc is None:
        return [f"{path}: empty file, skipped"]

    if kind == "plugin_config":
        try:
            changed, msg = _convert_endpoints_list_to_dict(doc)
        except Exception as e:
            return [f"{path}: ERROR migrating endpoints: {e}"]
        if changed:
            msgs.append(f"{path}: {msg}")
    elif kind == "main_config":
        per_plugin = _migrate_main_config(doc)
        if per_plugin:
            msgs.append(f"{path}:")
            msgs.extend(per_plugin)

    if msgs and not dry_run:
        try:
            with path.open("w", encoding="utf-8") as f:
                yaml.dump(doc, f)
        except Exception as e:
            msgs.append(f"{path}: ERROR writing: {e}")

    return msgs


def main():
    ap = argparse.ArgumentParser(description="PR2 config migration tool")
    ap.add_argument(
        "--root", type=Path, required=True, help="Tree root to migrate"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing"
    )
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.exists():
        sys.stderr.write(f"--root {root} does not exist\n")
        sys.exit(2)

    yaml = _make_yaml()
    targets = _iter_target_files(root)
    print(f"Scanning {root}: {len(targets)} target files")

    total_changes = 0
    for path, kind in targets:
        msgs = migrate_file(path, kind, dry_run=args.dry_run, yaml=yaml)
        for m in msgs:
            print(m)
        total_changes += len(msgs)

    suffix = " (dry run, no writes)" if args.dry_run else ""
    print(f"\nDone: {total_changes} change line(s) emitted{suffix}.")


if __name__ == "__main__":
    main()
