from configparser import SectionProxy
from dataclasses import dataclass
from typing import NamedTuple, Optional


@dataclass
class Origin:
    """Information about how to exec into an origin container."""

    namespace: str
    pod_name: str
    container_name: str
    context: str

    @property
    def deployment(self) -> str:
        return "-".join(self.pod_name.split("-")[:-2])


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
