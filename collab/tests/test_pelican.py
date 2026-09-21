from unittest.mock import patch

import pytest

from collab_types import InnerScriptError, Origin
from pelican import get_exports_for_pod


@patch("pelican.run_inner_script")
@patch("pelican.copy_inner_script_to_origin")
def test_get_exports_for_pod(mock_copy, mock_run_inner):
    origin = Origin(
        namespace="ns",
        pod_name="pod",
        container_name="c",
        context="ctx",
        deployment_name="dep",
    )

    # POSIX storage type should extract posix.exports
    mock_run_inner.return_value = {
        "status": "ok",
        "sitename": "site-posix",
        "storagetype": "posix",
        "posix": {"exports": [{"path": "/p1"}]},
        "time": "2023-01-01T00:00:00Z",
    }
    sitename, exports, time_str = get_exports_for_pod(origin)
    assert sitename == "site-posix"
    assert exports == [{"path": "/p1"}]
    assert time_str == "2023-01-01T00:00:00Z"

    # S3 storage type should extract s3.exports
    mock_run_inner.return_value = {
        "status": "ok",
        "sitename": "site-s3",
        "storagetype": "s3",
        "s3": {
            "serviceurl": "http://endpoint",
            "region": None,
            "accesskey": None,
            "secretkey": None,
            "exports": [{"s3bucket": "b1"}],
        },
        "time": "2023-01-01T00:00:00Z",
    }
    with patch(
        "pelican.handle_s3_exports", return_value=[{"s3bucket": "b1", "size": 1}]
    ):
        sitename, exports, time_str = get_exports_for_pod(origin)
    assert sitename == "site-s3"
    assert exports == [{"s3bucket": "b1", "size": 1}]
    assert time_str == "2023-01-01T00:00:00Z"

    # Unknown storage type should return empty exports
    mock_run_inner.return_value = {
        "status": "ok",
        "sitename": "site-unknown",
        "storagetype": "unknown",
        "time": "2023-01-01T00:00:00Z",
    }
    sitename, exports, time_str = get_exports_for_pod(origin)
    assert sitename == "site-unknown"
    assert exports == []
    assert time_str == "2023-01-01T00:00:00Z"

    # Error status in response should raise InnerScriptError
    mock_run_inner.return_value = {"status": "error", "error": "some error"}
    with pytest.raises(
        InnerScriptError, match="Inner script returned error: some error"
    ):
        get_exports_for_pod(origin)
