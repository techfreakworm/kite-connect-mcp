"""The docs stay true to the code: every tool name and every KITE_* setting they mention exists."""

from __future__ import annotations

import pathlib
import re

import pytest

from kite_connect_mcp.config import _FILE_VARIANTS, KNOWN_VARIABLES

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = sorted([ROOT / "README.md", *(ROOT / "docs").glob("*.md"), ROOT / ".env.example", ROOT / "SECURITY.md"])
TOOL_WORD = re.compile(
    r"`((?:get|place|modify|cancel|delete|create|search|refresh|convert|authorise|engage|complete|ticker|session)"
    r"_[a-z_]+|login|logout)`"
)
NOT_TOOLS = {"authorise_url"}  # result fields that happen to look like tool names
SETTING = re.compile(r"\b(KITE_[A-Z][A-Z0-9_]*[A-Z0-9])\b(?!\.md)")


@pytest.fixture(scope="module")
def tool_names(mcp_factory_module):
    _, reg, _ = mcp_factory_module
    return {info.name for info in reg.registered} | set(reg.skipped)


@pytest.fixture(scope="module")
def mcp_factory_module(tmp_path_factory):
    from conftest import write_token_file
    from kite_connect_mcp.config import Settings
    from kite_connect_mcp.server import build_runtime, build_server

    tmp = tmp_path_factory.mktemp("docs")
    env = {
        "KITE_API_KEY": "fakekey0001",
        "KITE_MCP_STATE_DIR": str(tmp / "state"),
        "KITE_MCP_CACHE_DIR": str(tmp / "cache"),
        "KITE_TOKEN_FILE": str(write_token_file(tmp / "state" / "t.json")),
        "KITE_MCP_ALLOW_MF_WRITES": "true",
    }
    rt = build_runtime(Settings.from_env(env))
    server, reg = build_server(rt)
    return server, reg, rt


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_tool_names_in_docs_exist(doc, tool_names):
    mentioned = set(TOOL_WORD.findall(doc.read_text(encoding="utf-8"))) - NOT_TOOLS
    assert not sorted(mentioned - tool_names), f"{doc.name} names tools that do not exist"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_settings_in_docs_exist(doc):
    mentioned = set(SETTING.findall(doc.read_text(encoding="utf-8")))
    unknown = sorted(m for m in mentioned - KNOWN_VARIABLES - _FILE_VARIANTS if not m.endswith("_FILE"))
    assert not unknown, f"{doc.name} names settings the server does not read"


def test_tool_reference_is_current(mcp_factory_module):
    """docs/TOOLS.md is what `KITE_MCP_ALLOW_MF_WRITES=true kite-connect-mcp tools --markdown` prints today."""
    from kite_connect_mcp.cli import tools_markdown

    server, reg, _ = mcp_factory_module
    generated = tools_markdown(server, reg)
    current = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    assert current.strip() == generated.strip(), (
        "docs/TOOLS.md is stale: run KITE_MCP_ALLOW_MF_WRITES=true kite-connect-mcp tools --markdown > docs/TOOLS.md"
    )
