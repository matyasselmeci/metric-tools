import datetime
import fnmatch
import json
import logging
from typing import Optional

from collab_types import T_CollabNSMap

DATA_MAX_AGE = datetime.timedelta(days=1)
_log = logging.getLogger(__name__)


def match_collab(fed_prefix: str, collab_ns_map: T_CollabNSMap) -> Optional[str]:
    """
    Return the collab name whose glob list fnmatch-matches fed_prefix, else None.

    Parameters
    ----------
    fed_prefix:
        The federation prefix to look up.
    collab_ns_map:
        A mapping of collaboration name to list of fnmatch glob patterns.

    Returns
    -------
    str | None
        The name of the collaboration (if found), else None.
    """
    for collab, patterns in collab_ns_map.items():
        if any(fnmatch.fnmatch(fed_prefix, p) for p in patterns):
            return collab
    return None


def _read_exports(
    data_path: str,
    exclude_ns_globs: Optional[list[str]],
    max_age: datetime.timedelta,
) -> dict[str, tuple[Optional[bool], int]]:
    """
    Read exports from a .jsonl file and return a deduplicated dict.

    Returns a mapping of federation_prefix -> (public, size), where the last
    entry for each federation_prefix wins (last-wins deduplication).
    Entries missing federation_prefix or size are skipped, as are entries
    whose federation_prefix matches any glob in exclude_ns_globs. Entries
    whose "time" field is missing, unparseable, or older than max_age
    are skipped entirely (all of their exports).

    Parameters
    ----------
    max_age:
        Maximum age of a "time" entry to still be included.
    """
    seen: dict[str, tuple[Optional[bool], int]] = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    with open(data_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            time_str = entry.get("time")
            try:
                entry_time = datetime.datetime.fromisoformat(time_str)
            except (TypeError, ValueError):
                _log.debug("%s: Skipping (missing or invalid time)", entry)
                continue
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=datetime.timezone.utc)
            if now - entry_time > max_age:
                _log.debug("%s: Skipping (time older than %s)", entry, max_age)
                continue
            for exp in entry.get("exports") or []:
                fed = exp.get("federation_prefix")
                size = exp.get("size")
                if fed is None:
                    _log.debug("%s: Skipping (no federation_prefix)", exp)
                    continue
                if size is None:
                    _log.debug("%s: Skipping (no size)", exp)
                    continue
                if exclude_ns_globs and any(
                    fnmatch.fnmatch(fed, g) for g in exclude_ns_globs
                ):
                    _log.debug("%s: Skipping (matches an exclude_ns_glob)", exp)
                    continue
                seen[fed] = (exp.get("public"), size)
    return seen


def _print_title(title: str) -> None:
    print(title)
    print("=" * len(title))
    print()


def print_exports_table(
    data_path: str,
    *,
    si: bool = False,
    collab_ns_map: Optional[T_CollabNSMap] = None,
    exclude_ns_globs: Optional[list[str]] = None,
    title: Optional[str] = None,
    max_age: datetime.timedelta = DATA_MAX_AGE,
) -> None:
    """
    Read a .jsonl file produced by this script and print a table of exports.

    Columns printed: federation_prefix, public, size (in TiB by default).
    Exports where any of those three fields is missing or null are skipped.
    Exports whose federation_prefix matches any glob in exclude_ns_globs are silently omitted.
    Entries whose "time" field is missing, unparseable, or older than max_age are omitted entirely.
    When the same federation_prefix appears more than once, the last entry wins.

    Parameters
    ----------
    data_path:
        Path to the data file to get the numbers from (e.g. ``"nautilus.jsonl"``).
    si:
        If True, display size in SI terabytes (10^12 bytes) instead of TiB (2^40 bytes).
    collab_ns_map:
        A collaboration-to-namespace glob pattern mapping.
    exclude_ns_globs:
        Glob patterns for federation prefixes to silently exclude from the table.
    max_age:
        Maximum age of a "time" entry to still be included. Defaults to DATA_MAX_AGE.
    """
    if title is not None:
        _print_title(title)

    divisor = 1e12 if si else 2**40
    size_header = "size (TB)" if si else "size (TiB)"

    use_collab = bool(collab_ns_map)
    seen = _read_exports(data_path, exclude_ns_globs, max_age)

    rows: list[tuple] = []
    for fed, (pub, size) in seen.items():
        if pub is None:
            continue
        if use_collab:
            assert collab_ns_map is not None
            collab = match_collab(fed, collab_ns_map) or "(unknown)"
        else:
            collab = "(unknown)"
        access = "public" if pub else "auth"
        rows.append((collab, fed, access, f"{size / divisor:.2f}"))

    if not rows:
        print("(no data)\n")
        return

    rows.sort(key=lambda r: (r[0], r[1]))
    headers = ("collab", "federation_prefix", "access", size_header)
    col_widths = [
        max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)
    ]
    alignments = ["<", "<", "<", ">"]
    fmt = "  ".join(f"{{:{a}{w}}}" for a, w in zip(alignments, col_widths))
    sep = "  ".join("-" * w for w in col_widths)
    print(fmt.format(*headers))
    print(sep)
    for row in rows:
        print(fmt.format(*row))
    print()


def print_collabs_summary(
    data_paths: list[str],
    collab_ns_map: T_CollabNSMap,
    *,
    si: bool = False,
    exclude_ns_globs: Optional[list[str]] = None,
    title: Optional[str] = None,
    max_age: datetime.timedelta = DATA_MAX_AGE,
) -> None:
    """
    Read one or more .jsonl files and print a combined per-collaboration storage summary.

    Aggregates export sizes by collaboration (via match_collab), tracking public and
    private bytes separately. Prints a three-column table sorted alphabetically with an
    (unknown) row at the bottom for unmatched prefixes, then lists each unmatched prefix
    followed by configured collaborations that have no matching data.

    Within each file, the same federation_prefix is deduplicated (last entry wins).
    Prefixes from different files are summed independently.
    Entries whose "time" field is missing, unparseable, or older than max_age are omitted entirely.

    Parameters
    ----------
    data_paths:
        Paths to the .jsonl data files to aggregate.
    collab_ns_map:
        A collaboration-to-namespace glob pattern mapping.
    si:
        If True, display size in SI terabytes (10^12 bytes) instead of TiB (2^40 bytes).
    exclude_ns_globs:
        Glob patterns for federation prefixes to silently exclude from the summary.
    max_age:
        Maximum age of a "time" entry to still be included. Defaults to DATA_MAX_AGE.
    """
    if title is not None:
        _print_title(title)

    divisor = 1e12 if si else 2**40
    unit = "TB" if si else "TiB"
    pub_header = f"public ({unit})"
    auth_header = f"auth ({unit})"

    totals: dict[str, list[int]] = {}  # collab -> [pub_bytes, auth_bytes]
    unknown_pub: int = 0
    unknown_auth: int = 0
    unmatched: set[str] = set()

    for data_path in data_paths:
        seen = _read_exports(data_path, exclude_ns_globs, max_age)
        for fed, (pub, size) in seen.items():
            if pub is None:
                continue
            collab = match_collab(fed, collab_ns_map)
            if collab is None:
                unmatched.add(fed)
                if pub:
                    unknown_pub += size
                else:
                    unknown_auth += size
            else:
                if collab not in totals:
                    totals[collab] = [0, 0]
                if pub:
                    totals[collab][0] += size
                else:
                    totals[collab][1] += size

    if not totals and not unmatched:
        print("(no data)\n")
    else:
        rows = [
            (collab, f"{auth / divisor:.2f}", f"{pub / divisor:.2f}")
            for collab, (pub, auth) in sorted(totals.items())
        ]
        if unmatched:
            rows.append(
                (
                    "(unknown)",
                    f"{unknown_auth / divisor:.2f}",
                    f"{unknown_pub / divisor:.2f}",
                )
            )

        headers = ("collab", auth_header, pub_header)
        col_widths = [
            max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)
        ]
        alignments = ["<", ">", ">"]
        fmt = "  ".join(f"{{:{a}{w}}}" for a, w in zip(alignments, col_widths))
        sep = "  ".join("-" * w for w in col_widths)
        print(fmt.format(*headers))
        print(sep)
        for row in rows:
            print(fmt.format(*row))
        print()

    if unmatched:
        print("Unmatched federation prefixes:")
        for prefix in sorted(unmatched):
            print(f"  {prefix}")

    missing_collabs = sorted(set(collab_ns_map) - set(totals))
    if missing_collabs:
        print("\nCollabs with no data:")
        for collab in missing_collabs:
            print(f"  {collab}")
