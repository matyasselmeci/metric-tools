#!/usr/bin/env python3
"""
storage_metrics

Collect and print Pelican Origin storage metrics gathered from
Kubernetes clusters via finding origin Deployments, execing into a pod
resolved from each,
and getting disk usage stats.  It supports the Nautilus, Tiger,
and Tempest clusters.

Requires `kubectl` to access the clusters.  Optionally, requires
the AWS CLI to query disk usage of S3 origins.  Your kubeconfig
must already have settings for the clusters you want to query, in
a separate context for each cluster.  The default context names are
"nautilus", "tiger", and "tempest", but that can be changed with the
--{nautilus,tiger,tempest}-context arguments.

Data usage is aggregated by federation prefix and then mapped to
collaborations based on globs in the "config.ini" file.  The collected
data is saved in JSONL files in an output/ directory and can be reused
in later runs via the --input argument, though by default input older
than 1 day is ignored.
"""

import argparse
import configparser
import datetime
import fnmatch
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from collab_types import ConfigData, T_Clusters, T_CollabNSMap, T_SubNSMap
from k8s import (
    check_cluster_access,
    check_namespace_access,
    find_pelican_origin_deployments,
)
from output import print_collabs_summary, print_exports_table
from pelican import get_exports_for_pod


def parse_args(argv) -> argparse.Namespace:
    """Parse and validate CLI arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--nautilus", action="store_true", help="Only process the nautilus cluster"
    )
    parser.add_argument(
        "--tiger", action="store_true", help="Only process the tiger cluster"
    )
    parser.add_argument(
        "--tempest", action="store_true", help="Only process the tempest cluster"
    )
    parser.add_argument(
        "--nautilus-context",
        metavar="CONTEXT",
        default=None,
        help="Override the kubectl context for the nautilus cluster",
    )
    parser.add_argument(
        "--tiger-context",
        metavar="CONTEXT",
        default=None,
        help="Override the kubectl context for the tiger cluster",
    )
    parser.add_argument(
        "--tempest-context",
        metavar="CONTEXT",
        default=None,
        help="Override the kubectl context for the tempest cluster",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N origins per cluster",
    )
    parser.add_argument(
        "-s",
        type=int,
        default=0,
        metavar="N",
        help="Skip the first N origins per cluster (to resume a previous run)",
    )
    parser.add_argument(
        "-p",
        "--pod",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Only process origins whose Deployment name starts with PREFIX "
        "(may be given multiple times)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        dest="verbose",
        action="store_false",
        help="Do not print progress messages to stderr",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug messages",
    )
    parser.add_argument(
        "--no-exports",
        dest="print_exports",
        action="store_false",
        help="Do not print the exports tables",
    )
    parser.add_argument(
        "--no-summary",
        dest="print_summary",
        action="store_false",
        help="Do not print the per-collaboration storage summary",
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        default=[],
        metavar="FILE",
        help="Read data from FILE instead of querying clusters (may be given multiple times)",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=1.0,
        metavar="DAYS",
        help="Maximum age (in days, may be fractional) of a line of data to include "
        "in the exports table and summary (default: 1)",
    )
    args = parser.parse_args(argv)

    if args.s and args.pod:
        parser.error("-s and --pod are mutually exclusive")

    return args


def read_config(args: argparse.Namespace) -> ConfigData:
    """Read config.ini and return cluster list, prefix maps, and exclusion globs."""
    any_cluster = args.nautilus or args.tiger or args.tempest
    run_nautilus = args.nautilus or not any_cluster
    run_tiger = args.tiger or not any_cluster
    run_tempest = args.tempest or not any_cluster

    cfg = configparser.ConfigParser()
    # preserve key case:
    cfg.optionxform = str  # type: ignore
    cfg.read("config.ini")

    clusters = []
    if run_nautilus and "nautilus" in cfg:
        if args.nautilus_context:
            cfg["nautlius"]["context"] = args.nautilus_context
        clusters.append(("nautilus", cfg["nautilus"]))
    if run_tiger and "tiger" in cfg:
        if args.tiger_context:
            cfg["tiger"]["context"] = args.tiger_context
        clusters.append(("tiger", cfg["tiger"]))
    if run_tempest and "tempest" in cfg:
        if args.tempest_context:
            cfg["tempest"]["context"] = args.tempest_context
        clusters.append(("tempest", cfg["tempest"]))

    # Parse [cluster:pod-prefix] sections for sub-namespace mapping
    sub_ns_map: T_SubNSMap = {}
    known_clusters = {"nautilus", "tiger", "tempest"}
    for section_name in cfg.sections():
        if ":" not in section_name:
            continue
        cluster_name, pod_prefix = section_name.split(":", 1)
        if cluster_name not in known_clusters:
            continue

        section = cfg[section_name]
        prefix_pairs: list[tuple[str, str]] = []

        n = 1
        while True:
            storage_key = f"storage_prefix_{n}"
            federation_key = f"federation_prefix_{n}"
            if storage_key not in section or federation_key not in section:
                break
            prefix_pairs.append((section[storage_key], section[federation_key]))
            n += 1

        if prefix_pairs:
            sub_ns_map[section_name] = prefix_pairs

    collab_ns_map: T_CollabNSMap = {}
    if "collab_namespaces" in cfg:
        for collab_name, globs_str in cfg["collab_namespaces"].items():
            collab_ns_map[collab_name] = globs_str.split()

    exclude_ns_globs: list[str] = []
    if "exclude_namespaces" in cfg:
        for _, globs_str in cfg["exclude_namespaces"].items():
            exclude_ns_globs.extend(globs_str.split())

    return ConfigData(
        clusters=clusters,
        sub_ns_map=sub_ns_map,
        collab_ns_map=collab_ns_map,
        exclude_ns_globs=exclude_ns_globs,
    )


def k8s_pre_flight_check(clusters: T_Clusters) -> bool:
    """
    Check if the requested clusters are reachable.

    Parameters
    ----------
    clusters:
        The definitions of the clusters to check, from config.ini.

    Returns
    -------
    bool
        True if the clusters are accessible.
    """
    inaccessible_clusters = []
    for cluster_name, section in clusters:
        context = section["context"]
        if not check_cluster_access(cluster_name, context):
            inaccessible_clusters.append(cluster_name)
    if inaccessible_clusters:
        print(
            "ERROR: cannot access cluster(s): " + ", ".join(inaccessible_clusters),
            file=sys.stderr,
        )
        return False
    return True


def gather_from_clusters(args: argparse.Namespace, config: ConfigData) -> list[str]:
    out_files = []

    for cluster_name, section in config.clusters:
        context = section["context"]
        namespaces = section["namespaces"].split()
        out_file = section["file"]
        exclude_globs = section.get("exclude_origins", "").split()
        cluster_count = 0
        cluster_skipped = 0
        cluster_eligible = 0
        cluster_excluded = 0

        with open(out_file, "a") as fh:
            for namespace in namespaces:
                # If -n is specified, stop after the given number of namespaces.
                if args.n is not None and cluster_count >= args.n:
                    break
                cluster_count, cluster_skipped, eligible, excluded = _process_namespace(
                    cluster_name,
                    context,
                    namespace,
                    fh,
                    args,
                    config.sub_ns_map,
                    cluster_count,
                    cluster_skipped,
                    exclude_globs,
                )
                cluster_eligible += eligible
                cluster_excluded += excluded

        if cluster_eligible > 0 and cluster_eligible == cluster_excluded:
            print(f"All pods for {cluster_name} skipped.", file=sys.stderr)

        out_files.append(out_file)

    # Render after collection so stdout tables cannot be interleaved with stderr progress.
    if args.verbose:
        print()
        print()

    return out_files


def print_tables_from_files(
    input_files: list[str],
    config: ConfigData,
    max_age: datetime.timedelta,
    print_exports: bool,
    print_summary: bool,
) -> bool:
    """
    Read input files and print all the requested tables.

    Parameters
    ----------
    input_flies:
        A list of input files to read data from.
    config:
        The ConfigData object containing various mappings.
    max_age:
        Data older than this will be ignored.
    print_exports:
        Print the individual exports tables.
    print_summary:
        Print the Storage Utilization summary table.

    Returns
    -------
    bool
        True if all input files were read successfully.
    """
    all_ok = True
    stems = Counter(Path(f).stem for f in input_files)
    for input_file in input_files:
        if print_exports:
            stem = Path(input_file).stem
            table_title = f"{stem.title()} Exports"
            if stems[stem] > 1:
                table_title += f" ({input_file})"
            try:
                print_exports_table(
                    input_file,
                    collab_ns_map=config.collab_ns_map,
                    exclude_ns_globs=config.exclude_ns_globs,
                    title=table_title,
                    max_age=max_age,
                )
            except OSError as err:
                print(f"Error loading {input_file}: {err}", file=sys.stderr)
                all_ok = False
            sys.stdout.flush()
    if print_summary:
        print_collabs_summary(
            input_files,
            config.collab_ns_map,
            exclude_ns_globs=config.exclude_ns_globs,
            title="Storage Utilization",
            max_age=max_age,
        )
        sys.stdout.flush()
    return all_ok


def _get_sub_ns_prefixes(
    sub_ns_map: T_SubNSMap,
    cluster_name: str,
    deployment_name: str,
) -> Optional[list[tuple[str, str]]]:
    """
    Find prefix pairs for an origin from the sub-namespace map.

    Iterates through sub_ns_map for keys starting with "cluster_name:".
    Returns the prefix list for the first key where
    deployment_name.startswith(deployment_prefix). Returns None if no match.

    Parameters
    ----------
    sub_ns_map:
        Dict mapping "CLUSTER:DEPLOYMENT_PREFIX" to lists of (storage_prefix, federation_prefix) tuples.
    cluster_name:
        The cluster name to search for.
    deployment_name:
        The Deployment name to match against deployment_prefix.

    Returns
    -------
    list[tuple[str, str]] | None
        The prefix pairs if a match is found, otherwise None.
    """
    prefix = f"{cluster_name}:"
    for key in sub_ns_map:
        if not key.startswith(prefix):
            continue
        deployment_prefix = key[len(prefix) :]
        if deployment_name.startswith(deployment_prefix):
            return sub_ns_map[key]
    return None


def _process_origin(
    cluster_name: str,
    origin,
    prefix_pairs: Optional[list[tuple[str, str]]],
    fh,
    args: argparse.Namespace,
) -> None:
    """Fetch exports for one origin and append the result to *fh*."""
    exports = None
    sitename = None
    time_str = None
    ok = True
    try:
        if args.verbose:
            print(
                f"[{cluster_name}] {origin.deployment_name}: Getting exports...",
                file=sys.stderr,
                flush=True,
            )
        if prefix_pairs is not None:
            sitename, exports, time_str = get_exports_for_pod(
                origin, prefix_pairs=prefix_pairs
            )
        else:
            sitename, exports, time_str = get_exports_for_pod(origin)
    except Exception as err:
        print(f"ERROR: {origin.deployment_name}: {err}", file=sys.stderr)
        ok = False

    if args.verbose:
        print(
            f"[{cluster_name}] {origin.deployment_name}: {'ok' if ok else 'FAIL'}",
            file=sys.stderr,
            flush=True,
        )

    fh.write(
        json.dumps(
            {
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "sitename": sitename,
                "origin": origin.deployment_name,
                "exports": exports,
            }
        )
        + "\n"
    )
    fh.flush()


def _process_namespace(
    cluster_name: str,
    context: str,
    namespace: str,
    fh,
    args: argparse.Namespace,
    sub_ns_map: T_SubNSMap,
    cluster_count: int,
    cluster_skipped: int,
    exclude_globs: Optional[list[str]] = None,
) -> tuple[int, int, int, int]:
    """
    Process all origins in one namespace: check access, list Deployments,
    apply filters, collect exports, and append results to *fh*.

    Returns
    -------
    tuple[int, int, int, int]
        Updated ``(cluster_count, cluster_skipped, eligible, excluded)`` where
        *eligible* is the number of origins that would have been processed and
        *excluded* is how many of those were silently skipped by *exclude_globs*.
    """
    if exclude_globs is None:
        exclude_globs = []

    if not check_namespace_access(cluster_name, context, namespace):
        return cluster_count, cluster_skipped, 0, 0

    try:
        origins = list(
            find_pelican_origin_deployments(context=context, namespace=namespace)
        )
    except Exception as err:
        print(
            f"ERROR: failed to list deployments in cluster={cluster_name!r} "
            f"namespace={namespace!r}: {err}",
            file=sys.stderr,
        )
        return cluster_count, cluster_skipped, 0, 0

    eligible = 0
    excluded = 0

    for origin in origins:
        if args.n is not None and cluster_count >= args.n:
            break

        explicitly_selected = bool(args.pod) and any(
            origin.deployment_name.startswith(p) for p in args.pod
        )
        if args.pod and not explicitly_selected:
            continue
        if cluster_skipped < args.s:
            cluster_skipped += 1
            continue

        prefix_pairs = _get_sub_ns_prefixes(
            sub_ns_map, cluster_name, origin.deployment_name
        )
        if prefix_pairs is None:
            continue

        eligible += 1

        if not explicitly_selected and any(
            fnmatch.fnmatch(origin.deployment_name, g) for g in exclude_globs
        ):
            excluded += 1
            continue

        _process_origin(cluster_name, origin, prefix_pairs, fh, args)
        cluster_count += 1

    return cluster_count, cluster_skipped, eligible, excluded


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    config = read_config(args)
    max_age = datetime.timedelta(days=args.max_age)

    if args.input:
        if not args.print_exports and not args.print_summary:
            print("Nothing to do")
            return 2

        #
        # Input files mode
        # Read existing data from input files only.
        #
        table_files = args.input

    else:

        #
        # Gather mode
        # Enter pods in clusters to gather statistics.
        #

        if not k8s_pre_flight_check(config.clusters):
            return 1

        table_files = gather_from_clusters(args, config)

    if print_tables_from_files(
        table_files, config, max_age, args.print_exports, args.print_summary
    ):
        return 0
    else:
        return 1

    return 0


if __name__ == "__main__":
    ret = main()
    sys.stdout.flush()
    sys.exit(ret)
