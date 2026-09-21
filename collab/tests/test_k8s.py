import json
from unittest.mock import MagicMock, patch

import pytest

from collab_types import Error, Origin
from k8s import (
    _resolve_pod_for_deployment,
    check_cluster_access,
    check_namespace_access,
    examine_deployment,
    find_pelican_origin_deployments,
    is_origin_container,
    namespace_for_context,
)


def test_is_origin_container():
    # Matching images for pelican origin or osdf-origin
    assert (
        is_origin_container(
            {"image": "hub.opensciencegrid.org/pelican/osdf-origin:latest"}
        )
        is True
    )
    assert (
        is_origin_container({"image": "hub.opensciencegrid.org/pelican/origin:v1.0.0"})
        is True
    )

    # Non-matching images should return False
    assert (
        is_origin_container({"image": "hub.opensciencegrid.org/pelican/cache:latest"})
        is False
    )
    assert is_origin_container({"image": "nginx:latest"}) is False

    # Images with fewer than 3 / separated parts (no registry)
    # The implementation does parts = re.split(r"[:@/]", full_image); image = parts[2]
    # So "pelican/origin" -> ["pelican", "origin"] -> IndexError
    assert is_origin_container({"image": "pelican/origin"}) is False
    assert is_origin_container({"image": "origin"}) is False

    # Missing image key should return False
    assert is_origin_container({}) is False


def test_examine_deployment():
    # Mock _current_context, namespace_for_context, and pod resolution to
    # avoid subprocess calls
    with (
        patch("k8s._current_context", return_value="my-context"),
        patch("k8s.namespace_for_context", return_value="my-ns"),
        patch("k8s._resolve_pod_for_deployment", return_value="dep-1-abc12-xyz34"),
    ):

        # Deployment with origin container should be recognized and returned
        deployment_origin = {
            "metadata": {"name": "dep-1"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "c1",
                                "image": "hub.opensciencegrid.org/pelican/osdf-origin:latest",
                            }
                        ]
                    }
                }
            },
        }
        origin = examine_deployment(deployment_origin)
        assert origin == Origin(
            namespace="my-ns",
            pod_name="dep-1-abc12-xyz34",
            container_name="c1",
            context="my-context",
            deployment_name="dep-1",
        )

        # Deployment with non-origin container should return None
        deployment_no_origin = {
            "metadata": {"name": "dep-1"},
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": "c2", "image": "nginx:latest"}]}
                }
            },
        }
        assert examine_deployment(deployment_no_origin) is None

        # Deployment missing metadata/spec should return None
        assert examine_deployment({}) is None

    # If no Running pod can be resolved for a qualifying Deployment, return
    # None (there's nothing we could exec into).
    with (
        patch("k8s._current_context", return_value="my-context"),
        patch("k8s.namespace_for_context", return_value="my-ns"),
        patch("k8s._resolve_pod_for_deployment", return_value=None),
    ):
        assert examine_deployment(deployment_origin) is None


@patch("k8s.run")
def test_resolve_pod_for_deployment(mock_run):
    deployment = {
        "spec": {"selector": {"matchLabels": {"app": "dep-1"}}},
    }

    # A Running pod matching the selector is returned
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "items": [
                    {"metadata": {"name": "dep-1-abc12-xyz34"}},
                    {"metadata": {"name": "dep-1-abc12-xyz00"}},
                ]
            }
        ),
    )
    pod_name = _resolve_pod_for_deployment("ctx", "ns", deployment)
    # Sorted alphabetically so the lowest-sorting name wins deterministically
    assert pod_name == "dep-1-abc12-xyz00"
    cmd = mock_run.call_args.args[0]
    assert "--selector" in cmd
    assert cmd[cmd.index("--selector") + 1] == "app=dep-1"

    # No matching pods: None
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({"items": []}))
    assert _resolve_pod_for_deployment("ctx", "ns", deployment) is None

    # kubectl failure: None
    mock_run.return_value = MagicMock(returncode=1, stdout="")
    assert _resolve_pod_for_deployment("ctx", "ns", deployment) is None

    # No selector on the deployment: None, and kubectl isn't even called
    mock_run.reset_mock()
    assert _resolve_pod_for_deployment("ctx", "ns", {"spec": {}}) is None
    mock_run.assert_not_called()


@patch("k8s._resolve_pod_for_deployment", return_value="dep-1-abc12-xyz34")
@patch("k8s.run")
def test_find_pelican_origin_deployments(mock_run, mock_resolve):
    with (
        patch("k8s._current_context", return_value="my-context"),
        patch("k8s.namespace_for_context", return_value="my-ns"),
    ):
        deployment_ready = {
            "metadata": {"name": "dep-ready"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "c1", "image": "example.com/pelican/origin:v1"}
                        ]
                    }
                }
            },
            "status": {"readyReplicas": 1},
        }
        # Not ready: should be skipped without even trying to resolve a pod
        deployment_not_ready = {
            "metadata": {"name": "dep-not-ready"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "c1", "image": "example.com/pelican/origin:v1"}
                        ]
                    }
                }
            },
            "status": {"readyReplicas": 0},
        }
        # Not an origin: should be skipped
        deployment_other = {
            "metadata": {"name": "dep-other"},
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": "c1", "image": "nginx:latest"}]}
                }
            },
            "status": {"readyReplicas": 1},
        }

        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                {
                    "items": [
                        deployment_ready,
                        deployment_not_ready,
                        deployment_other,
                    ]
                }
            )
        )

        origins = list(find_pelican_origin_deployments())
        assert origins == [
            Origin(
                namespace="my-ns",
                pod_name="dep-1-abc12-xyz34",
                container_name="c1",
                context="my-context",
                deployment_name="dep-ready",
            )
        ]


@patch("k8s.run")
def test_namespace_for_context(mock_run):
    # Active context with * marker should parse correctly
    mock_run.return_value = MagicMock(stdout="*  ctx-1  cluster-1  user-1  ns-1\n")
    assert namespace_for_context("ctx-1") == "ns-1"

    # Non-active context should also parse correctly
    mock_run.return_value = MagicMock(stdout="   ctx-2  cluster-2  user-2  ns-2\n")
    assert namespace_for_context("ctx-2") == "ns-2"

    # Unparseable output should raise Error
    mock_run.return_value = MagicMock(stdout="bad line\n")
    with pytest.raises(Error, match="Could not determine namespace"):
        namespace_for_context("ctx-3")


@patch("k8s.run")
def test_check_namespace_access(mock_run, capsys):
    # All checks passing should return True
    mock_run.return_value = MagicMock(returncode=0, stdout="yes")
    assert check_namespace_access("cluster", "context", "ns") is True

    # First check failing should return False and print error
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout="", stderr="forbidden"),
        MagicMock(returncode=0, stdout="yes"),
        MagicMock(returncode=0, stdout="yes"),
    ]
    assert check_namespace_access("cluster", "context", "ns") is False
    captured = capsys.readouterr().err
    assert "ERROR: insufficient permissions" in captured
    assert "forbidden" in captured


@patch("k8s.run")
def test_check_cluster_access_checks_both_permissions(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="yes", stderr="")

    assert check_cluster_access("cluster", "context") is True
    assert mock_run.call_count == 3
    assert mock_run.call_args_list[0].args[0] == [
        "kubectl",
        "--context",
        "context",
        "auth",
        "can-i",
        "get",
        "deployments",
    ]
    assert mock_run.call_args_list[1].args[0] == [
        "kubectl",
        "--context",
        "context",
        "auth",
        "can-i",
        "get",
        "pods",
    ]


@patch("k8s.run")
def test_check_cluster_access_reports_both_failures(mock_run, capsys):
    mock_run.side_effect = [
        MagicMock(returncode=1, stdout="", stderr="forbidden"),
        MagicMock(returncode=1, stdout="no", stderr=""),
        MagicMock(returncode=0, stdout="yes", stderr=""),
    ]

    assert check_cluster_access("cluster", "context") is False
    assert mock_run.call_count == 3
    captured = capsys.readouterr().err
    assert captured.count("ERROR: insufficient access") == 2
    assert "forbidden" in captured
    assert "no" in captured
