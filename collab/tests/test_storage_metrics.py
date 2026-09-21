import argparse
import datetime
import json
from unittest.mock import call, patch

import pytest

from collab_types import Origin
from storage_metrics import (
    _process_namespace,
    _process_origin,
    main,
    parse_args,
    read_config,
)


def test_parse_args():
    # Default flags
    args = parse_args([])
    assert args.verbose is True
    assert args.print_exports is True
    assert args.print_summary is True
    assert args.input == []
    assert args.nautilus is False
    assert args.tiger is False
    assert args.tempest is False

    # Cluster flags are set correctly
    args = parse_args(["--nautilus"])
    assert args.nautilus is True
    assert args.tiger is False

    # Context override flags
    args = parse_args(["--nautilus-context", "foo"])
    assert args.nautilus_context == "foo"
    assert args.tiger_context is None
    assert args.tempest_context is None

    # New flags
    args = parse_args(["--no-exports", "--no-summary"])
    assert args.print_exports is False
    assert args.print_summary is False

    # -s and --pod together should raise parser error (mutually exclusive)
    with pytest.raises(SystemExit):
        parse_args(["-s", "10", "--pod", "some-pod"])


def test_read_config_clusters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.ini"
    config_file.write_text("[nautilus]\ncontext = c1\n[tiger]\ncontext = c2\n")

    # No cluster flags: returns all clusters present in config
    args = parse_args([])
    config = read_config(args)
    assert len(config.clusters) == 2
    assert config.clusters[0][0] == "nautilus"
    assert config.clusters[1][0] == "tiger"
    assert isinstance(config.sub_ns_map, dict)
    assert config.collab_ns_map == {}
    assert config.exclude_ns_globs == []

    # Single cluster flag: only that cluster
    args = parse_args(["--nautilus"])
    config = read_config(args)
    assert len(config.clusters) == 1
    assert config.clusters[0][0] == "nautilus"


def test_read_config_collab_map(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.ini"

    # Single prefix per collab
    config_file.write_text(
        "[collab_namespaces]\nEHT = /EHT/public\nREDTOP = /REDTOP/public\n"
    )
    config = read_config(parse_args([]))
    assert config.collab_ns_map == {
        "EHT": ["/EHT/public"],
        "REDTOP": ["/REDTOP/public"],
    }

    # Multiple space-separated prefixes
    config_file.write_text(
        "[collab_namespaces]\nEvent_Horizon_Telescope = /EHT/public /EHT/private\n"
    )
    config = read_config(parse_args([]))
    assert config.collab_ns_map == {
        "Event_Horizon_Telescope": ["/EHT/public", "/EHT/private"]
    }

    # Empty [collab_namespaces] section
    config_file.write_text("[collab_namespaces]\n")
    config = read_config(parse_args([]))
    assert config.collab_ns_map == {}


def test_read_config_exclude_namespaces(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "config.ini"

    # Absent section: empty list
    config_file.write_text("")
    config = read_config(parse_args([]))
    assert config.exclude_ns_globs == []

    # Single key with one glob
    config_file.write_text(
        "[exclude_namespaces]\nmigrated = /ospool/uc-shared/*/HepSim\n"
    )
    config = read_config(parse_args([]))
    assert config.exclude_ns_globs == ["/ospool/uc-shared/*/HepSim"]

    # Multiple keys and multiple space-separated globs per key: all collected flat
    config_file.write_text(
        "[exclude_namespaces]\n"
        "migrated = /ospool/uc-shared/*/HepSim /ospool/uc-shared/*/REDTOP\n"
        "untracked = /jkb-lab /ndp/*\n"
    )
    config = read_config(parse_args([]))
    assert set(config.exclude_ns_globs) == {
        "/ospool/uc-shared/*/HepSim",
        "/ospool/uc-shared/*/REDTOP",
        "/jkb-lab",
        "/ndp/*",
    }


@patch("storage_metrics.get_exports_for_pod")
@patch("storage_metrics.find_pelican_origin_deployments")
@patch("storage_metrics.check_namespace_access")
def test_process_namespace_verbose(
    mock_access, mock_find, mock_exports, tmp_path, capsys
):
    mock_access.return_value = True
    origin = Origin(
        namespace="ns",
        pod_name="collab-shared-osdf-pelican-origin-abc-7f9c9b6d84-xyz12",
        container_name="c",
        context="ctx",
        deployment_name="collab-shared-osdf-pelican-origin-abc",
    )
    mock_find.return_value = [origin]
    mock_exports.return_value = ("site1", [], "2023-01-01T00:00:00Z")

    sub_ns_map = {
        "tiger:collab-shared-osdf-pelican-origin": [
            ("/mnt/origin/public", "/ospool/uw-shared/public")
        ]
    }
    out_file = tmp_path / "out.jsonl"

    # verbose=True: before and after messages on stderr, nothing on stdout
    args = argparse.Namespace(n=None, s=0, pod=[], verbose=True)
    with open(out_file, "w") as fh:
        _process_namespace("tiger", "ctx", "ns", fh, args, sub_ns_map, 0, 0)
    captured = capsys.readouterr()
    assert "tiger" in captured.err
    assert "collab-shared-osdf-pelican-origin-abc" in captured.err
    assert captured.out == ""

    # verbose=False: no stderr progress output
    args.verbose = False
    with open(out_file, "w") as fh:
        _process_namespace("tiger", "ctx", "ns", fh, args, sub_ns_map, 0, 0)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
def test_main_input_flag(mock_table, mock_summary, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = out.jsonl\n"
    )
    f1 = tmp_path / "a.jsonl"
    f2 = tmp_path / "b.jsonl"
    f1.write_text("")
    f2.write_text("")

    # -i alone implies both table and summary; clusters are not queried
    # table is called once per file; summary is called once with all files combined
    main(["-i", str(f1), "-i", str(f2)])
    assert mock_table.call_count == 2
    assert mock_summary.call_count == 1
    table_files = [c[0][0] for c in mock_table.call_args_list]
    assert table_files == [str(f1), str(f2)]
    assert mock_summary.call_args[0][0] == [str(f1), str(f2)]
    # Titles derived from stems: f1="a", f2="b" — no duplicates, no path disambiguation
    titles = [c[1]["title"] for c in mock_table.call_args_list]
    assert titles == ["A Exports", "B Exports"]
    assert mock_summary.call_args[1]["title"] == "Storage Utilization"


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", return_value=True)
def test_main_default_behavior(
    mock_access, mock_process, mock_table, mock_summary, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    out_file = str(tmp_path / "nautilus.jsonl")
    (tmp_path / "config.ini").write_text(
        f"[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = {out_file}\n"
    )
    mock_process.return_value = (1, 0, 1, 0)

    # Without any flag, both are printed
    main(["--nautilus"])
    mock_table.assert_called_once_with(
        out_file,
        collab_ns_map={},
        exclude_ns_globs=[],
        title="Nautilus Exports",
        max_age=datetime.timedelta(days=1),
    )
    mock_summary.assert_called_once_with(
        [out_file],
        {},
        exclude_ns_globs=[],
        title="Storage Utilization",
        max_age=datetime.timedelta(days=1),
    )


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", return_value=True)
def test_main_context_override(
    mock_access, mock_process, mock_table, mock_summary, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    out_file = str(tmp_path / "nautilus.jsonl")
    (tmp_path / "config.ini").write_text(
        f"[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = {out_file}\n"
    )
    mock_process.return_value = (1, 0, 1, 0)

    # Without override, config context is used
    main(["--nautilus"])
    assert mock_process.call_args.args[1] == "c1"

    # With override, the flag wins
    main(["--nautilus", "--nautilus-context", "override-ctx"])
    assert mock_process.call_args.args[1] == "override-ctx"


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", return_value=True)
def test_main_renders_tables_after_all_clusters_are_collected(
    mock_access, mock_process, mock_table, mock_summary, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    nautilus_file = str(tmp_path / "nautilus.jsonl")
    tiger_file = str(tmp_path / "tiger.jsonl")
    (tmp_path / "config.ini").write_text(
        "[nautilus]\n"
        f"context = c1\nnamespaces = ns1\nfile = {nautilus_file}\n"
        "[tiger]\n"
        f"context = c2\nnamespaces = ns2\nfile = {tiger_file}\n"
    )
    mock_process.return_value = (1, 0, 1, 0)
    events = []
    mock_process.side_effect = lambda *args: (
        events.append(f"fetch:{args[0]}") or (1, 0, 1, 0)
    )
    mock_table.side_effect = lambda *_args, **kwargs: events.append(
        f"table:{kwargs['title'].split()[0].lower()}"
    )
    mock_summary.side_effect = lambda *_args, **_kwargs: events.append("summary")

    main([])

    assert events == [
        "fetch:nautilus",
        "fetch:tiger",
        "table:nautilus",
        "table:tiger",
        "summary",
    ]


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", return_value=True)
def test_main_no_flags(
    mock_access, mock_process, mock_table, mock_summary, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    out_file = str(tmp_path / "nautilus.jsonl")
    (tmp_path / "config.ini").write_text(
        f"[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = {out_file}\n"
    )
    mock_process.return_value = (1, 0, 1, 0)

    # --no-exports suppresses table but NOT summary
    main(["--nautilus", "--no-exports"])
    mock_table.assert_not_called()
    mock_summary.assert_called_once()

    # --no-summary suppresses summary but NOT table
    mock_table.reset_mock()
    mock_summary.reset_mock()
    main(["--nautilus", "--no-summary"])
    mock_table.assert_called_once()
    mock_summary.assert_not_called()

    # Both "no" flags suppress both
    mock_table.reset_mock()
    mock_summary.reset_mock()
    main(["--nautilus", "--no-exports", "--no-summary"])
    mock_table.assert_not_called()
    mock_summary.assert_not_called()


@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", side_effect=[False, True, False])
def test_main_checks_all_clusters_before_collection(
    mock_access, mock_process, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = nautilus.jsonl\n"
        "[tiger]\ncontext = c2\nnamespaces = ns2\nfile = tiger.jsonl\n"
        "[tempest]\ncontext = c3\nnamespaces = ns3\nfile = tempest.jsonl\n"
    )

    assert main([]) == 1
    assert [call.args for call in mock_access.call_args_list] == [
        ("nautilus", "c1"),
        ("tiger", "c2"),
        ("tempest", "c3"),
    ]
    mock_process.assert_not_called()
    assert "nautilus, tempest" in capsys.readouterr().err


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
def test_main_input_flag_defaults(mock_table, mock_summary, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = out.jsonl\n"
    )
    f = tmp_path / "data.jsonl"
    f.write_text("")

    # -i with no other flags: both table and summary
    main(["-i", str(f)])
    mock_table.assert_called_once()
    mock_summary.assert_called_once()

    # -i with --no-exports explicitly: only summary, not table
    mock_table.reset_mock()
    mock_summary.reset_mock()
    main(["-i", str(f), "--no-exports"])
    mock_table.assert_not_called()
    mock_summary.assert_called_once()

    # -i with --no-summary explicitly: only table, not summary
    mock_table.reset_mock()
    mock_summary.reset_mock()
    main(["-i", str(f), "--no-summary"])
    mock_table.assert_called_once()
    mock_summary.assert_not_called()


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
def test_main_nothing_to_do(mock_table, mock_summary, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = out.jsonl\n"
    )
    f = tmp_path / "data.jsonl"
    f.write_text("")

    # -i with both --no flags: "Nothing to do"
    assert main(["-i", str(f), "--no-exports", "--no-summary"]) == 2
    assert "Nothing to do" in capsys.readouterr().out
    mock_table.assert_not_called()
    mock_summary.assert_not_called()


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
def test_main_input_title_disambiguation(
    mock_table, mock_summary, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = out.jsonl\n"
    )
    # Two files with the same stem "data" in different directories
    d1 = tmp_path / "dir1"
    d2 = tmp_path / "dir2"
    d1.mkdir()
    d2.mkdir()
    f1 = d1 / "data.jsonl"
    f2 = d2 / "data.jsonl"
    f1.write_text("")
    f2.write_text("")

    main(["-i", str(f1), "-i", str(f2)])
    titles = [c[1]["title"] for c in mock_table.call_args_list]
    # Duplicate stems → full path appended
    assert titles[0] == f"Data Exports ({str(f1)})"
    assert titles[1] == f"Data Exports ({str(f2)})"


@patch("storage_metrics.print_collabs_summary")
@patch("storage_metrics.print_exports_table")
@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", return_value=True)
def test_main_cluster_title(
    mock_access, mock_process, mock_table, mock_summary, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    out_file = str(tmp_path / "nautilus.jsonl")
    (tmp_path / "config.ini").write_text(
        f"[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = {out_file}\n"
    )
    mock_process.return_value = (1, 0, 1, 0)

    main(["--nautilus"])
    assert mock_table.call_args[1]["title"] == "Nautilus Exports"
    assert mock_summary.call_args[1]["title"] == "Storage Utilization"


@patch("storage_metrics.get_exports_for_pod")
@patch("storage_metrics.find_pelican_origin_deployments")
@patch("storage_metrics.check_namespace_access")
def test_process_namespace_exclude(mock_access, mock_find, mock_exports, tmp_path):
    mock_access.return_value = True
    mock_exports.return_value = ("site1", [], "2023-01-01T00:00:00Z")

    origin_excl = Origin(
        namespace="ns",
        pod_name="nsdf-origin-abc-def-p1",
        container_name="c",
        context="ctx",
        deployment_name="nsdf-origin-abc-def",
    )
    origin_kept = Origin(
        namespace="ns",
        pod_name="my-origin-abc-def-p1",
        container_name="c",
        context="ctx",
        deployment_name="my-origin-abc-def",
    )
    mock_find.return_value = [origin_excl, origin_kept]

    args = argparse.Namespace(n=None, s=0, pod=[], verbose=False)
    out_file = tmp_path / "out.jsonl"

    # nsdf-origin* matches the glob; my-origin* does not
    with open(out_file, "w") as fh:
        count, _, eligible, excluded = _process_namespace(
            "nautilus", "ctx", "ns", fh, args, {}, 0, 0, exclude_globs=["nsdf-origin*"]
        )
    assert eligible == 2
    assert excluded == 1
    assert count == 1
    assert mock_exports.call_count == 1

    # -p selects the excluded pod explicitly: exclusion does not apply
    mock_exports.reset_mock()
    args_p = argparse.Namespace(n=None, s=0, pod=["nsdf-origin"], verbose=False)
    with open(out_file, "w") as fh:
        count, _, eligible, excluded = _process_namespace(
            "nautilus", "ctx", "ns", fh, args_p, {}, 0, 0, exclude_globs=["nsdf-origin*"]
        )
    assert excluded == 0
    assert count == 1


@patch("storage_metrics.print_exports_table")
@patch("storage_metrics._process_namespace")
@patch("storage_metrics.check_cluster_access", return_value=True)
def test_main_all_pods_skipped(
    mock_access, mock_process, mock_table, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    out_file = str(tmp_path / "nautilus.jsonl")
    (tmp_path / "config.ini").write_text(
        f"[nautilus]\ncontext = c1\nnamespaces = ns1\nfile = {out_file}\nexclude_origins = some-origin\n"
    )

    # All eligible pods excluded: warning printed
    mock_process.return_value = (0, 0, 3, 3)
    main(["--nautilus"])
    assert "All pods for nautilus skipped." in capsys.readouterr().err

    # Only some excluded: no warning
    mock_process.return_value = (1, 0, 3, 2)
    main(["--nautilus"])
    assert "All pods for nautilus skipped." not in capsys.readouterr().err

    # No eligible pods at all: no warning
    mock_process.return_value = (0, 0, 0, 0)
    main(["--nautilus"])
    assert "All pods for nautilus skipped." not in capsys.readouterr().err


def test_process_origin_adds_date(tmp_path):
    origin = Origin(
        namespace="ns",
        pod_name="dep-abc-123",
        container_name="c",
        context="ctx",
        deployment_name="dep",
    )
    args = argparse.Namespace(verbose=False)
    out_file = tmp_path / "out.jsonl"

    with patch("storage_metrics.get_exports_for_pod") as mock_get:
        mock_get.return_value = (
            "site",
            [{"federation_prefix": "/f", "size": 100}],
            "2023-01-01T00:00:00Z",
        )
        with open(out_file, "w") as fh:
            _process_origin("cluster", origin, None, fh, args)

    with open(out_file) as fh:
        line = fh.readline()
        data = json.loads(line)
        assert "time" in data
        assert data["origin"] == "dep"
        # Check ISO8601 format: parseable by datetime.fromisoformat
        date_str = data["time"]
        dt = datetime.datetime.fromisoformat(date_str)
        assert dt is not None


def test_process_origin_adds_date_on_failure(tmp_path):
    origin = Origin(
        namespace="ns",
        pod_name="dep-abc-123",
        container_name="c",
        context="ctx",
        deployment_name="dep",
    )
    args = argparse.Namespace(verbose=False)
    out_file = tmp_path / "out.jsonl"

    with patch("storage_metrics.get_exports_for_pod") as mock_get:
        mock_get.side_effect = Exception("failed")
        with open(out_file, "w") as fh:
            _process_origin("cluster", origin, None, fh, args)

    with open(out_file) as fh:
        line = fh.readline()
        data = json.loads(line)
        assert data["time"] is not None
        assert data["origin"] == "dep"
        assert data["exports"] is None
        dt = datetime.datetime.fromisoformat(data["time"])
        assert dt is not None
