import json
import sys
from typing import Optional

from collab_types import Export, InnerScriptError, Origin
from helpers import run
from k8s import run_in_origin
from s3 import handle_s3_exports

INNER_SCRIPT = "inner.py"


def get_origin_export_dirs(
    origin: Origin,
) -> list[Export]:
    """
    Run ``<binary> config dump --json`` inside the Origin container and parse
    the exported storage directories, splitting them by access type.

    Two config formats are supported and may both be present simultaneously;
    results from each are merged into the returned lists.

    New-style (``Origin.Exports``):
        Each export entry carries its own ``capabilities`` list.  An entry is
        classified as *public* if ``"PublicReads"`` appears in that list;
        otherwise it is *authenticated*.

    Old-style (``Origin.ExportVolumes``):
        Each element is a string of the form ``"<storage_path>:<federation_path>"``.
        The left-hand side is the storage prefix.  Items where either side is
        not an absolute path (i.e. does not start with ``/``) are ignored.
        All valid storage prefixes share the same access type, determined by
        ``Origin.EnablePublicReads``: ``true`` → public, ``false`` (or absent)
        → authenticated.

    ``Origin.EnablePublicReads`` has no effect on new-style ``Origin.Exports``
    entries.

    Parameters
    ----------
    origin:
        A `Origin` identifying the namespace, pod, container,
        and binary to use.

    Returns
    -------
    list[Export]
        A list of each export.

    Raises
    ------
    subprocess.CalledProcessError
        If the ``kubectl exec`` call fails.
    json.JSONDecodeError
        If the binary does not return valid JSON.
    """
    result = run_in_origin(
        origin,
        [
            "sh",
            "-c",
            "bin=$(command -v pelican-server 2>/dev/null || command -v osdf-server 2>/dev/null || command -v pelican 2>/dev/null); "
            "$bin config dump --json",
        ],
    )

    config: dict = json.loads(result.stdout)
    origin_cfg: dict = config.get("Origin") or {}

    exports: list[Export] = []

    # ------------------------------------------------------------------
    # New-style: Origin.Exports
    # Each entry carries its own capabilities list.
    # ------------------------------------------------------------------
    for export in origin_cfg.get("Exports") or []:
        try:
            capabilities: list[str] = export["capabilities"]
            storage_prefix: str = export["storageprefix"]
            federation_prefix: str = export["federationprefix"]
        except KeyError:
            # Malformed export
            continue

        exports.append(
            Export(
                storage_prefix=storage_prefix,
                federation_prefix=federation_prefix,
                public=("PublicReads" in capabilities),
            )
        )

    # ------------------------------------------------------------------
    # Old-style: Origin.ExportVolumes
    # Access type is uniform across all entries, governed by
    # Origin.EnablePublicReads.
    # ------------------------------------------------------------------
    export_volumes: list[str] = origin_cfg.get("ExportVolumes") or []
    volumes_are_public: bool = bool(origin_cfg.get("EnablePublicReads"))

    for volume in export_volumes:
        if volume.count(":") != 1:
            # Malformed volume
            continue
        storage_prefix, _, federation_prefix = volume.partition(":")
        if not storage_prefix.startswith("/") or not federation_prefix.startswith("/"):
            continue

        exports.append(
            Export(
                storage_prefix=storage_prefix,
                federation_prefix=federation_prefix,
                public=volumes_are_public,
            )
        )

    return exports


def copy_inner_script_to_origin(origin: Origin):
    """
    Copy the inner script to the temp directory in *origin*
    so it can be run later.
    """
    return run(
        [
            "kubectl",
            "--context",
            origin.context,
            "cp",
            "-c",
            origin.container_name,
            INNER_SCRIPT,
            f"{origin.namespace}/{origin.pod_name}:/tmp/{INNER_SCRIPT}",
        ]
    )


def run_inner_script(origin: Origin, *args: str, copy=True) -> dict:
    """
    Runs the inner script in the pod on the storage path, returning the result.

    Parameters
    ----------
    origin:
        The origin to run the script inside.
    args:
        Arguments to the script.
    copy:
        True if we should copy the inner script first.

    Returns
    -------
    dict
        The results from running the script.

    Raises
    ------
    InnerScriptError
        If something goes wrong with the inner script.
    """
    if copy:
        copy_inner_script_to_origin(origin)
    ret = run_in_origin(origin, [f"/tmp/{INNER_SCRIPT}"] + list(args), check=False)
    try:
        results = json.loads(ret.stdout)
    except json.JSONDecodeError as err:
        raise InnerScriptError(
            f"inner script failed to return parseable output: {err}.\n"
            f"stdout: {ret.stdout}\n"
            f"stderr: {ret.stderr}\n"
        ) from err

    return results


def get_exports_for_pod(
    origin: Origin, prefix_pairs: Optional[list[tuple[str, str]]] = None
) -> tuple[str, list[dict], str]:
    """
    Copy ``inner.py`` into *origin*, run it, and return the sitename and exports.

    Two modes, selected by *prefix_pairs*:

    Auto-discovery (``prefix_pairs=None``):
        ``inner.py`` is invoked with no arguments.  It discovers exports on its
        own — used for Nautilus pods, where the storage layout is self-describing.

    Scan mode (``prefix_pairs`` provided):
        ``inner.py`` is invoked as ``inner.py scan <storage>:<federation> ...``
        with one argument per pair.  Used for Tiger/Tempest pods, where the
        storage-to-federation prefix mapping is known from the config.

    The export dicts in the returned list come directly from the inner script:
    POSIX exports include at minimum ``federation_prefix``, ``public``, and
    ``size`` (bytes); S3 exports have the same shape after ``handle_s3_exports``
    resolves bucket sizes.

    Parameters
    ----------
    origin:
        The pod to exec into.
    prefix_pairs:
        List of ``(storage_prefix, federation_prefix)`` tuples for scan mode.
        Pass ``None`` for auto-discovery.

    Returns
    -------
    str
        The site name reported by the origin.
    list[dict]
        The list of export dicts (empty list for unknown storage types).
    time
        The ISO8601 timestamp of the data gather time (as reported
        by the inner script).

    Raises
    ------
    InnerScriptError
        If ``inner.py`` returns non-JSON output or a ``status != "ok"`` result.
    subprocess.CalledProcessError
        If any ``kubectl`` call fails.
    """
    copy_inner_script_to_origin(origin)
    if prefix_pairs is not None:
        args = ["scan"] + [f"{s}:{f}" for s, f in prefix_pairs]
        result = run_inner_script(origin, *args, copy=False)
    else:
        result = run_inner_script(origin, copy=False)
    if result['status'] != "ok":
        raise InnerScriptError(f"Inner script returned error: {result['error']}")
    storagetype = result['storagetype']
    if storagetype in {"posix", "posixv2"}:
        exports = result['posix']['exports']
    elif storagetype in {"s3", "s3v2"}:
        exports = handle_s3_exports(result['s3'])
    else:
        print(
            f"WARNING: {origin.deployment_name}: unknown or unsupported storage type {storagetype!r}, skipping exports",
            file=sys.stderr,
        )
        exports = []
    return result['sitename'], exports, result['time']
