import fnmatch
import json
import re
import subprocess
import sys
from collections.abc import Generator
from typing import Optional

from collab_types import Error, Origin
from helpers import run

# Image name substrings that identify a Pelican Origin container.
ORIGIN_IMAGE_NAMES: tuple[str, ...] = ("osdf-origin", "origin")


def run_in_origin(
    origin: Origin,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess:

    cmd = [
        "kubectl",
        "--context",
        origin.context,
        "exec",
        "--namespace",
        origin.namespace,
        "--container",
        origin.container_name,
        origin.pod_name,
    ]
    return run(cmd + ["--"] + args, check=check)


def check_namespace_access(cluster: str, context: str, namespace: str) -> bool:
    """
    Return True if we have permission to get deployments, get pods, and exec
    into pods in *namespace*. Prints an error to stderr and returns False if
    any check fails.
    """
    checks = [
        [
            "kubectl",
            "--context",
            context,
            "auth",
            "can-i",
            "get",
            "deployments",
            "--namespace",
            namespace,
        ],
        [
            "kubectl",
            "--context",
            context,
            "auth",
            "can-i",
            "get",
            "pods",
            "--namespace",
            namespace,
        ],
        [
            "kubectl",
            "--context",
            context,
            "auth",
            "can-i",
            "create",
            "pods/exec",
            "--namespace",
            namespace,
        ],
    ]
    for cmd in checks:
        ret = run(cmd, check=False)
        if ret.returncode != 0 or ret.stdout.strip() != "yes":
            print(
                f"ERROR: insufficient permissions in cluster={cluster!r} "
                f"namespace={namespace!r}: {' '.join(cmd[5:])} -> "
                f"{ret.stdout.strip() or ret.stderr.strip()}",
                file=sys.stderr,
            )
            return False
    return True


def check_cluster_access(cluster: str, context: str) -> bool:
    """
    Return True if the current credentials can access the cluster context.

    All permissions are checked even when an earlier one fails so callers can
    report the complete access status before starting data collection.
    """
    checks = [
        ["kubectl", "--context", context, "auth", "can-i", "get", "deployments"],
        ["kubectl", "--context", context, "auth", "can-i", "get", "pods"],
        [
            "kubectl",
            "--context",
            context,
            "auth",
            "can-i",
            "create",
            "pods/exec",
        ],
    ]
    access = True
    for cmd in checks:
        ret = run(cmd, check=False)
        if ret.returncode != 0 or ret.stdout.strip() != "yes":
            print(
                f"ERROR: insufficient access to cluster={cluster!r}: "
                f"{' '.join(cmd[5:])} -> "
                f"{ret.stdout.strip() or ret.stderr.strip()}",
                file=sys.stderr,
            )
            access = False
    return access


def _current_context() -> str:
    """Return the current Kubernetes context, or raise Error if it cannot be determined."""
    ret = run(["kubectl", "config", "current-context"])
    context = ret.stdout.strip()
    if not context:
        raise Error("Could not determine current Kubernetes context")
    return context


def namespace_for_context(context: Optional[str] = None) -> str:
    """
    Return the namespace configured for *context*, or raise Error if it cannot
    be determined.  If *context* is not given, uses the current context.
    """
    if context is None:
        context = _current_context()
    ret = run(["kubectl", "config", "get-contexts", "--no-headers", context])
    for line in ret.stdout.splitlines():
        parts = re.split(r"\s+", line.strip())
        # Strip leading '*' marker for the active context
        if parts and parts[0] == "*":
            parts = parts[1:]
        # Columns: NAME CLUSTER AUTHINFO [NAMESPACE]
        if len(parts) >= 4:
            return parts[3]
    raise Error(f"Could not determine namespace for context {context!r}")


def is_origin_container(container: dict) -> bool:
    """
    Return True if *container* (a Kubernetes container spec dict) looks like a
    Pelican Origin container based on its image name (one of ORIGIN_IMAGE_NAMES).
    """
    full_image: str = container.get("image", "")
    # This assumes that image names always have the registry
    parts = re.split(r"[:@/]", full_image)
    try:
        image = parts[2]
        return image in ORIGIN_IMAGE_NAMES
    except IndexError:
        return False


def _resolve_pod_for_deployment(
    context: str,
    namespace: str,
    deployment: dict,
) -> Optional[str]:
    """
    Resolve one real, Running pod name that belongs to *deployment*, using the
    Deployment's ``spec.selector.matchLabels`` as a label selector.

    We can't use ``kubectl exec deploy/NAME`` for the whole workflow because
    ``kubectl cp`` requires a concrete pod name, and separate ``kubectl exec
    deploy/NAME`` invocations aren't guaranteed to land on the same pod. So we
    resolve a single pod once here and reuse it for every cp/exec call for
    this Origin.

    Only ``matchLabels`` selectors are supported (``matchExpressions`` are
    ignored); this matches the label conventions used by these Deployments'
    labels.

    Returns
    -------
    str | None
        The name of a Running pod matching the Deployment's selector, or None
        if the selector can't be determined or no Running pod matches.
    """
    match_labels: dict = (
        deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    )
    if not match_labels:
        return None

    selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    result = run(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "pods",
            "--namespace",
            namespace,
            "--selector",
            selector,
            "--field-selector",
            "status.phase=Running",
            "--output",
            "json",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None

    try:
        pod_list: dict = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    pods: list[dict] = pod_list.get("items", [])
    pod_names = sorted(
        p["metadata"]["name"] for p in pods if p.get("metadata", {}).get("name")
    )
    return pod_names[0] if pod_names else None


def examine_deployment(
    deployment: dict,
    context: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Optional[Origin]:
    """
    Examine a single Deployment manifest (as returned by the Kubernetes API /
    kubectl) and determine whether its pod template hosts a Pelican Origin
    container.

    If it does, resolve one real Running pod belonging to the Deployment (see
    :func:`_resolve_pod_for_deployment`) and return an :class:`Origin`.
    Returns *None* if the Deployment does not contain a recognised Pelican
    Origin container, or if no Running pod could be resolved for it.

    Parameters
    ----------
    deployment:
        A dict representing the Deployment's JSON manifest (e.g. from
        ``kubectl get deployment <n> -o json``).
    context:
        The Kubernetes context.  Defaults to the current context.
    namespace:
        The Kubernetes namespace.  Defaults to the namespace configured for
        *context*.

    Returns
    -------
    Origin | None
    """
    if context is None:
        context = _current_context()
    if namespace is None:
        namespace = namespace_for_context(context)

    try:
        deployment_name: str = deployment["metadata"]["name"]
        containers: list[dict] = deployment["spec"]["template"]["spec"]["containers"]
    except KeyError:
        return None

    for container in containers:
        if not is_origin_container(container):
            continue

        container_name: str = container["name"]

        pod_name = _resolve_pod_for_deployment(context, namespace, deployment)
        if pod_name is None:
            print(
                f"Origin {deployment_name!r}: could not resolve a Running pod",
                file=sys.stderr,
                flush=True,
            )
            return None

        return Origin(
            namespace=namespace,
            pod_name=pod_name,
            container_name=container_name,
            context=context,
            deployment_name=deployment_name,
        )

    return None


def find_pelican_origin_deployments(
    context: Optional[str] = None,
    namespace: Optional[str] = None,
    exclude_origins: Optional[list[str]] = None,
) -> Generator[Origin]:
    """
    List all Deployments in *namespace* and return information about every
    Deployment whose pod template contains a Pelican Origin container with a
    discoverable Pelican Server binary.

    Discovery works at the Deployment level rather than the pod level: for
    each qualifying, ready Deployment, one real pod is resolved (via the
    Deployment's label selector) and used for all subsequent cp/exec calls.
    This mirrors letting Kubernetes "pick" a pod from the Deployment, while
    guaranteeing the pod used for the inner-script copy step and the pod used
    to run it are the same.

    Parameters
    ----------
    context:
        The Kubernetes context to use.  Defaults to the current context.
    namespace:
        The Kubernetes namespace to search.  Defaults to the namespace
        configured for *context*.
    exclude_origins:
        Glob patterns for Deployment names that should be ignored before
        readiness warnings or pod resolution.

    Yields
    ------
    Origin
        The info about one Deployment: the namespace, a resolved pod name,
        container name, and the Deployment name.

    Raises
    ------
    Error
        If the context or namespace cannot be determined.
    subprocess.CalledProcessError
        If the initial ``kubectl get deployments`` call fails (e.g. bad
        namespace, missing credentials).
    json.JSONDecodeError
        If kubectl returns unexpected output.
    """
    if context is None:
        context = _current_context()
    if namespace is None:
        namespace = namespace_for_context(context)

    result = run(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "deployments",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
    )

    deployment_list: dict = json.loads(result.stdout)
    deployments: list[dict] = deployment_list.get("items", [])

    for deployment in deployments:
        deployment_name = deployment.get("metadata", {}).get("name", "<unknown>")
        if exclude_origins and any(
            fnmatch.fnmatch(deployment_name, pattern) for pattern in exclude_origins
        ):
            continue

        # A Deployment with 0 ready replicas has no pod we could exec into,
        # so skip it before even trying to resolve a pod (avoids a
        # 'could not resolve a Running pod' false alarm for scaled-down
        # Origins).
        ready_replicas = deployment.get("status", {}).get("readyReplicas", 0)
        if ready_replicas < 1:
            containers = (
                deployment.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            )
            if any(is_origin_container(container) for container in containers):
                print(
                    f"Origin {deployment_name!r}: not ready (readyReplicas={ready_replicas!r})",
                    file=sys.stderr,
                    flush=True,
                )
            continue

        info = examine_deployment(deployment, context, namespace)
        if info is not None:
            yield info


def interactive_exec(origin: Origin, cmd=("bash",)) -> int:
    """
    Debugging function: interactively exec into an origin to take a look around.

    Parameters
    ----------
    origin
        The origin to exec into

    cmd
        The command to run

    Returns
    -------
    int
        The return code of the process.
    """
    if isinstance(cmd, str):
        cmd = [cmd]
    proc = subprocess.Popen(
        [
            "kubectl",
            "--context",
            origin.context,
            "exec",
            "--namespace",
            origin.namespace,
            "--container",
            origin.container_name,
            "-it",
            origin.pod_name,
            "--",
        ]
        + list(cmd)
    )
    return proc.wait()
