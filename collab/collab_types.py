from configparser import SectionProxy
from dataclasses import dataclass
from typing import NamedTuple, Optional


@dataclass
class Origin:
    """
    Information about how to exec into an origin container.

    Origins are discovered at the Deployment level (see k8s.py), but
    `kubectl cp` requires a concrete pod name and repeated `kubectl exec
    deploy/NAME` calls aren't guaranteed to land on the same pod. So
    *pod_name* is a single real pod resolved from the Deployment (via its
    label selector) once at discovery time, and reused for every cp/exec
    call against this Origin to guarantee they hit the same pod.
    """

    namespace: str
    pod_name: str
    container_name: str
    context: str
    deployment_name: str


@dataclass
class Export:
    """A storage prefix/federation prefix combo, plus whether it's public or not."""

    storage_prefix: str
    federation_prefix: str
    public: bool
    size: Optional[int] = None


class Error(Exception):
    """Base exception class"""


class InnerScriptError(Error):
    """Something went wrong with the inner script executed inside the container"""


T_Clusters = list[tuple[str, SectionProxy]]
T_CollabNSMap = dict[str, list[str]]
T_SubNSMap = dict[str, list[tuple[str, str]]]


class ConfigData(NamedTuple):
    clusters: T_Clusters
    collab_ns_map: T_CollabNSMap
    exclude_ns_globs: list[str]
    sub_ns_map: T_SubNSMap
