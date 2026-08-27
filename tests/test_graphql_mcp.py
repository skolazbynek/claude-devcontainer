"""Tests for cld.mcp.graphql MCP tools (thin client over the broker's `graphql` action)."""

import json
from unittest.mock import patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import cld.mcp.graphql as graphql_mod
from cld.mcp.graphql import (
    _format_type_ref,
    _summarize_schema,
    describe_type,
    get_server_logs,
    introspect,
    list_endpoints,
    query,
    restart_server,
    server_status,
    start_server,
    stop_server,
)


def _cp(returncode=0, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


@pytest.fixture(autouse=True)
def _reset_cache():
    graphql_mod._set_cached_schema(None)
    yield
    graphql_mod._set_cached_schema(None)


class TestLifecycleArgv:
    def test_start_server_no_args(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="running\t8000\thttp://x\tabc\tcld_gql_x\n")) as op:
            start_server()
        op.assert_called_once_with("start")

    def test_stop_server_no_args(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="stopped\n")) as op:
            stop_server()
        op.assert_called_once_with("stop")

    def test_restart_server_no_args(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="running\t8000\thttp://x\tabc\tcld_gql_x\n")) as op:
            restart_server()
        op.assert_called_once_with("restart")

    def test_server_status_no_args(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="not_started\t\t\t\t\n")) as op:
            server_status()
        op.assert_called_once_with("status")

    def test_get_server_logs_forwards_tail(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="a\nb\n")) as op:
            get_server_logs(tail=20)
        op.assert_called_once_with("logs", "20")

    def test_list_endpoints_no_args(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="dev\nstaging\n")) as op:
            out = list_endpoints()
        op.assert_called_once_with("endpoints")
        assert out == ["dev", "staging"]

    def test_query_forwards_target_query_and_json_variables(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout='{"data": {}}')) as op:
            query("query { me }", variables={"id": 1}, target="dev")
        op.assert_called_once_with("query", "dev", "query { me }", json.dumps({"id": 1}))

    def test_query_defaults_target_local_and_empty_variables(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout='{"data": {}}')) as op:
            query("{ __typename }")
        op.assert_called_once_with("query", "local", "{ __typename }", "{}")

    def test_introspect_forwards_target(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout='{"data": {"__schema": {"types": []}}}')) as op:
            introspect(target="staging")
        op.assert_called_once_with("introspect", "staging")


class TestBrokerFailurePropagates:
    def test_nonzero_returncode_raises_tool_error(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(returncode=3, stderr="denied: bad target")):
            with pytest.raises(ToolError, match="denied: bad target"):
                server_status()


class TestStatusLineParsing:
    def test_running_fresh(self):
        with patch("cld.mcp.graphql.graphql_op",
                    return_value=_cp(stdout="running\t32768\thttp://172.17.0.1:32768/graphql\tabc123\tcld_gql_x\tfalse\n")):
            out = server_status()
        assert out == {
            "status": "running", "port": 32768, "endpoint": "http://172.17.0.1:32768/graphql",
            "revision": "abc123", "container": "cld_gql_x", "stale": False,
        }

    def test_running_stale(self):
        with patch("cld.mcp.graphql.graphql_op",
                    return_value=_cp(stdout="running\t32768\thttp://172.17.0.1:32768/graphql\tabc123\tcld_gql_x\ttrue\n")):
            out = server_status()
        assert out["stale"] is True

    def test_not_started_blank_fields(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="not_started\t\t\t\t\t\n")):
            out = server_status()
        assert out == {
            "status": "not_started", "port": None, "endpoint": None, "revision": None,
            "container": None, "stale": None,
        }

    def test_malformed_short_line_pads_missing_fields(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="running\n")):
            out = server_status()
        assert out == {
            "status": "running", "port": None, "endpoint": None, "revision": None,
            "container": None, "stale": None,
        }

    def test_empty_output_reports_unknown(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="")):
            out = server_status()
        assert out["status"] == "unknown"
        assert out["stale"] is None


class TestGetServerLogs:
    def test_filters_by_pattern(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="INFO starting\nERROR boom\nINFO ready\n")):
            out = get_server_logs(filter_pattern="error")
        assert out == ["ERROR boom"]

    def test_invalid_regex_returns_error_line(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="a\nb\n")):
            out = get_server_logs(filter_pattern="[unclosed")
        assert len(out) == 1 and "Invalid regex" in out[0]

    def test_no_pattern_returns_all_lines(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="a\nb\nc\n")):
            out = get_server_logs()
        assert out == ["a", "b", "c"]


class TestDescribeType:
    def test_raises_without_a_cached_schema(self):
        with pytest.raises(ToolError, match="No cached schema"):
            describe_type("User")

    def test_raises_for_unknown_type(self):
        graphql_mod._set_cached_schema({"data": {"__schema": {"types": [{"name": "User"}]}}})
        with pytest.raises(ToolError, match="not found"):
            describe_type("Ghost")

    def test_returns_the_matching_type(self):
        graphql_mod._set_cached_schema({"data": {"__schema": {"types": [{"name": "User", "kind": "OBJECT"}]}}})
        assert describe_type("User") == {"name": "User", "kind": "OBJECT"}


class TestIntrospect:
    def test_populates_the_cache_and_returns_a_summary(self):
        raw = {
            "data": {
                "__schema": {
                    "queryType": {"name": "Query"},
                    "mutationType": None,
                    "types": [
                        {"name": "Query", "fields": [
                            {"name": "me", "args": [], "type": {"name": "User", "kind": "OBJECT"}},
                        ]},
                    ],
                }
            }
        }
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout=json.dumps(raw))):
            summary = introspect()
        assert summary == {"queries": ["me(): User"], "mutations": []}
        assert graphql_mod._get_cached_schema() == raw

    def test_non_json_response_raises_tool_error(self):
        with patch("cld.mcp.graphql.graphql_op", return_value=_cp(stdout="not json")):
            with pytest.raises(ToolError, match="not valid JSON"):
                introspect()


class TestFormatTypeRef:
    def test_none(self):
        assert _format_type_ref(None) == "?"

    def test_named(self):
        assert _format_type_ref({"kind": "SCALAR", "name": "String"}) == "String"

    def test_non_null(self):
        assert _format_type_ref({"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "ID"}}) == "ID!"

    def test_list_of_non_null(self):
        t = {"kind": "LIST", "ofType": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}}
        assert _format_type_ref(t) == "[Int!]"


class TestSummarizeSchema:
    def test_separates_queries_and_mutations_and_skips_introspection_types(self):
        raw = {
            "__schema": {
                "queryType": {"name": "Query"},
                "mutationType": {"name": "Mutation"},
                "types": [
                    {"name": "__Type", "fields": [{"name": "x", "args": [], "type": {"name": "String"}}]},
                    {"name": "Query", "fields": [
                        {"name": "me", "args": [], "type": {"kind": "NON_NULL", "ofType": {"name": "User"}}},
                    ]},
                    {"name": "Mutation", "fields": [
                        {"name": "login", "args": [{"name": "password", "type": {"name": "String"}}], "type": {"name": "Boolean"}},
                    ]},
                    {"name": "User", "fields": []},
                ],
            }
        }
        out = _summarize_schema(raw)
        assert out == {
            "queries": ["me(): User!"],
            "mutations": ["login(password: String): Boolean"],
        }

    def test_unwraps_the_data_envelope(self):
        raw = {"data": {"__schema": {"queryType": None, "mutationType": None, "types": []}}}
        assert _summarize_schema(raw) == {"queries": [], "mutations": []}
