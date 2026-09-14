"""Structural validation of the n8n workflow files.

An n8n workflow that imports but does not run is worse than one that fails to
import, because the failure shows up mid-trade. These tests check the things a
broken export actually gets wrong: dangling connections, unreachable nodes,
malformed expressions, duplicated webhook paths — and that no secret was ever
written into the JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "n8n" / "workflows"

TRIGGER_TYPES = {
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.errorTrigger",
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.executeWorkflowTrigger",
}
NON_EXECUTABLE = {"n8n-nodes-base.stickyNote"}

# Anything that looks like a real credential must never appear in a workflow file.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[\"']?\s*[:=]\s*[\"'][A-Za-z0-9/+_-]{16,}"),
)


def workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.json"))
    assert files, f"no workflow files found in {WORKFLOW_DIR}"
    return files


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflows() -> dict[str, dict]:
    return {path.name: load(path) for path in workflow_files()}


def test_expected_workflows_exist(workflows: dict[str, dict]) -> None:
    expected = {
        "01-market-data-collection.json",
        "02-market-analysis.json",
        "03-ai-trade-evaluation.json",
        "04-risk-validation.json",
        "05-trade-execution.json",
        "06-position-monitoring.json",
        "07-daily-report.json",
        "08-emergency-shutdown.json",
        "09-error-handler.json",
    }
    assert expected.issubset(set(workflows))


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_workflow_structure(workflows: dict[str, dict], filename: str) -> None:
    workflow = workflows[filename]
    for key in ("name", "nodes", "connections", "settings"):
        assert key in workflow, f"{filename} is missing '{key}'"
    assert workflow["active"] is False, "workflows must ship inactive"
    assert workflow["settings"]["executionOrder"] == "v1"

    names = [node["name"] for node in workflow["nodes"]]
    assert len(names) == len(set(names)), f"{filename} has duplicate node names"

    for node in workflow["nodes"]:
        for key in ("id", "name", "type", "typeVersion", "position", "parameters"):
            assert key in node, f"{filename}:{node.get('name')} missing '{key}'"
        assert isinstance(node["position"], list) and len(node["position"]) == 2
        assert node["type"].startswith("n8n-nodes-base."), node["type"]


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_connections_reference_existing_nodes(
    workflows: dict[str, dict], filename: str
) -> None:
    workflow = workflows[filename]
    names = {node["name"] for node in workflow["nodes"]}
    for source, outputs in workflow["connections"].items():
        assert source in names, f"{filename}: connection from unknown node '{source}'"
        for branch in outputs.get("main", []):
            for connection in branch:
                assert connection["node"] in names, (
                    f"{filename}: '{source}' connects to unknown node "
                    f"'{connection['node']}'"
                )
                assert connection["type"] == "main"


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_every_node_is_reachable_from_a_trigger(
    workflows: dict[str, dict], filename: str
) -> None:
    """An unreachable node is dead config that silently never runs."""
    workflow = workflows[filename]
    executable = [
        node for node in workflow["nodes"] if node["type"] not in NON_EXECUTABLE
    ]
    triggers = [node["name"] for node in executable if node["type"] in TRIGGER_TYPES]
    assert triggers, f"{filename} has no trigger node"

    reachable: set[str] = set()
    queue = list(triggers)
    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for branch in workflow["connections"].get(current, {}).get("main", []):
            queue.extend(connection["node"] for connection in branch)

    unreachable = {node["name"] for node in executable} - reachable
    assert not unreachable, f"{filename} has unreachable nodes: {sorted(unreachable)}"


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_if_nodes_have_valid_conditions(workflows: dict[str, dict], filename: str) -> None:
    for node in workflows[filename]["nodes"]:
        if node["type"] != "n8n-nodes-base.if":
            continue
        conditions = node["parameters"]["conditions"]
        assert conditions["combinator"] in ("and", "or")
        assert conditions["options"]["version"] == 2
        assert conditions["conditions"], f"{filename}:{node['name']} has no conditions"
        for condition in conditions["conditions"]:
            assert "leftValue" in condition and "operator" in condition
            operator = condition["operator"]
            assert operator["type"] in (
                "string",
                "number",
                "boolean",
                "array",
                "object",
                "dateTime",
            )
            assert operator["operation"]


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_http_nodes_are_well_formed(workflows: dict[str, dict], filename: str) -> None:
    for node in workflows[filename]["nodes"]:
        if node["type"] != "n8n-nodes-base.httpRequest":
            continue
        parameters = node["parameters"]
        url = parameters.get("url")
        assert url, f"{filename}:{node['name']} has no URL"
        # Any URL built from an expression must be flagged with '=' or n8n treats
        # it as a literal string containing braces.
        if "{{" in url:
            assert url.startswith("="), f"{filename}:{node['name']} URL missing '=' prefix"
        if parameters.get("sendBody"):
            assert parameters.get("specifyBody") == "json"
            body = parameters.get("jsonBody", "")
            assert body.startswith("="), (
                f"{filename}:{node['name']} jsonBody must be an expression"
            )
        assert isinstance(parameters.get("options", {}).get("timeout", 1), int)


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_expressions_are_prefixed(workflows: dict[str, dict], filename: str) -> None:
    """``{{ }}`` only evaluates when the value starts with ``=``."""

    def walk(value, path: str) -> None:
        if isinstance(value, str):
            if "{{" in value and not value.startswith("="):
                raise AssertionError(
                    f"{filename}:{path} contains an expression without '=' prefix: {value[:80]}"
                )
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    for node in workflows[filename]["nodes"]:
        # Code nodes contain JS template literals, which legitimately use ${}
        # and may contain {{ }} inside strings.
        if node["type"] == "n8n-nodes-base.code":
            continue
        walk(node["parameters"], node["name"])


def test_webhook_paths_are_unique(workflows: dict[str, dict]) -> None:
    seen: dict[str, str] = {}
    for filename, workflow in workflows.items():
        for node in workflow["nodes"]:
            if node["type"] != "n8n-nodes-base.webhook":
                continue
            path = node["parameters"]["path"]
            assert path not in seen, (
                f"webhook path '{path}' used by both {seen[path]} and {filename}"
            )
            seen[path] = filename
    # The chain depends on these exact paths existing.
    assert {
        "market-analysis",
        "ai-evaluation",
        "risk-validation",
        "trade-execution",
        "emergency-shutdown",
    }.issubset(set(seen))


def test_cross_workflow_calls_target_existing_webhooks(workflows: dict[str, dict]) -> None:
    """Chaining is by webhook path, so a typo would be a silent dead end."""
    defined = {
        node["parameters"]["path"]
        for workflow in workflows.values()
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.webhook"
    }
    called: set[str] = set()
    for filename, workflow in workflows.items():
        for node in workflow["nodes"]:
            url = node["parameters"].get("url", "")
            if "webhook/" in url:
                path = url.split("webhook/")[-1].strip()
                assert path in defined, (
                    f"{filename}:{node['name']} calls unknown webhook path '{path}'"
                )
                called.add(path)
    assert called, "no cross-workflow calls found — the pipeline would not chain"


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_no_secrets_in_workflow_files(filename: str) -> None:
    raw = (WORKFLOW_DIR / filename).read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        match = pattern.search(raw)
        assert match is None, f"{filename} contains a possible secret: {match.group()[:40]}"
    # Secrets must arrive via credentials or env, never as literals.
    assert "$env." in raw or "genericCredentialType" in raw


@pytest.mark.parametrize("filename", [path.name for path in workflow_files()])
def test_api_calls_are_authenticated(workflows: dict[str, dict], filename: str) -> None:
    """Every call to the trading API must carry auth."""
    for node in workflows[filename]["nodes"]:
        if node["type"] != "n8n-nodes-base.httpRequest":
            continue
        url = node["parameters"].get("url", "")
        if "TRADING_API_BASE_URL" not in url:
            continue
        assert node["parameters"].get("authentication") == "genericCredentialType", (
            f"{filename}:{node['name']} calls the trading API without authentication"
        )
        assert node["parameters"].get("genericAuthType") == "httpHeaderAuth"
        assert "httpHeaderAuth" in node.get("credentials", {})


def test_internal_webhooks_validate_the_shared_secret(workflows: dict[str, dict]) -> None:
    """Webhook-triggered workflows must reject unauthenticated callers."""
    for filename, workflow in workflows.items():
        webhook_nodes = [
            node for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.webhook"
        ]
        if not webhook_nodes:
            continue
        code_nodes = [
            node["parameters"].get("jsCode", "")
            for node in workflow["nodes"]
            if node["type"] == "n8n-nodes-base.code"
        ]
        assert any("x-workflow-secret" in code for code in code_nodes), (
            f"{filename} exposes a webhook without validating x-workflow-secret"
        )


def test_execution_workflow_requires_a_risk_approval(workflows: dict[str, dict]) -> None:
    """The execution stage must never place an order without an approval id."""
    workflow = workflows["05-trade-execution.json"]
    code_nodes = [
        node["parameters"].get("jsCode", "")
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.code"
    ]
    assert any("risk_approval_id" in code and "throw" in code for code in code_nodes)

    order_nodes = [
        node
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.httpRequest"
        and "/paper/order" in node["parameters"].get("url", "")
    ]
    assert order_nodes, "no order placement node found"
    for node in order_nodes:
        assert "risk_approval_id" in node["parameters"]["jsonBody"]


def test_failure_paths_report_errors(workflows: dict[str, dict]) -> None:
    """Every workflow that can fail routes the failure to the error sink."""
    for filename, workflow in workflows.items():
        if filename.startswith(("08", "09")):
            continue  # these *are* the failure paths
        urls = [
            node["parameters"].get("url", "")
            for node in workflow["nodes"]
            if node["type"] == "n8n-nodes-base.httpRequest"
        ]
        assert any("/workflow/error" in url for url in urls), (
            f"{filename} has no error-reporting path"
        )
