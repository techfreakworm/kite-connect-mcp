"""Release metadata stays consistent: package version, server.json (MCP Registry) and the CLI agree."""

from __future__ import annotations

import json
import pathlib
import tomllib

import kite_connect_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_server_json_matches_the_package():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == kite_connect_mcp.__version__
    assert server["name"] == "io.github.techfreakworm/kite-connect-mcp"
    assert len(server["description"]) <= 100  # the registry's limit
    pypi = [p for p in server["packages"] if p["registryType"] == "pypi"]
    assert pypi and pypi[0]["identifier"] == "kite-connect-mcp"
    assert pypi[0]["version"] == kite_connect_mcp.__version__


def test_pyproject_names_the_cli_and_licence():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "kite-connect-mcp"
    assert project["scripts"]["kite-connect-mcp"] == "kite_connect_mcp.cli:main"
    assert project["license"] == "MIT"
