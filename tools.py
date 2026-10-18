"""LangChain tools for investigating incidents in the FalkorDB graph."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import Counter
import math
import os
from typing import Any

from dotenv import load_dotenv
from falkordb import FalkorDB
from langchain_core.tools import tool


load_dotenv()

_MAX_HISTORICAL_INCIDENTS = 20
_MAX_OWNER_AND_RUNBOOK_RESULTS = 50

_db: FalkorDB | None = None
_graph: Any | None = None


def _streamlit_secret(name: str) -> str | None:
    """Read an optional Streamlit secret without requiring cloud configuration."""
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except (FileNotFoundError, KeyError, RuntimeError):
        return None

    if value is None:
        return None
    value_text = str(value).strip()
    return value_text or None


def _setting(name: str, default: str | None = None) -> str | None:
    """Read local environment configuration, then Streamlit secrets."""
    value = os.getenv(name) or _streamlit_secret(name)
    return value.strip() if value else default


def _setting_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean connection setting."""
    value = _setting(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "on"}


def _connection_kwargs() -> dict[str, Any]:
    """Build only supported FalkorDB connection options from configuration."""
    kwargs: dict[str, Any] = {
        "host": _setting("FALKORDB_HOST", "localhost"),
        "port": int(_setting("FALKORDB_PORT", "6379") or "6379"),
        "ssl": _setting_bool("FALKORDB_SSL"),
    }

    optional_settings = {
        "username": "FALKORDB_USERNAME",
        "password": "FALKORDB_PASSWORD",
        "ssl_cert_reqs": "FALKORDB_SSL_CERT_REQS",
        "ssl_ca_certs": "FALKORDB_SSL_CA_CERTS",
        "ssl_ca_path": "FALKORDB_SSL_CA_PATH",
        "ssl_ca_data": "FALKORDB_SSL_CA_DATA",
        "ssl_certfile": "FALKORDB_SSL_CERTFILE",
        "ssl_keyfile": "FALKORDB_SSL_KEYFILE",
        "ssl_password": "FALKORDB_SSL_PASSWORD",
        "ssl_ocsp_expected_cert": "FALKORDB_SSL_OCSP_EXPECTED_CERT",
    }
    for option, setting_name in optional_settings.items():
        value = _setting(setting_name)
        if value is not None:
            kwargs[option] = value

    if _setting("FALKORDB_SSL_CHECK_HOSTNAME") is not None:
        kwargs["ssl_check_hostname"] = _setting_bool(
            "FALKORDB_SSL_CHECK_HOSTNAME"
        )
    for option, setting_name in {
        "ssl_validate_ocsp": "FALKORDB_SSL_VALIDATE_OCSP",
        "ssl_validate_ocsp_stapled": "FALKORDB_SSL_VALIDATE_OCSP_STAPLED",
    }.items():
        if _setting(setting_name) is not None:
            kwargs[option] = _setting_bool(setting_name)

    return kwargs


def _get_graph() -> Any:
    """Return the lazily initialized shared FalkorDB graph."""
    global _db, _graph

    if _graph is None:
        url = _setting("FALKORDB_URL")
        if url:
            url_kwargs = _connection_kwargs()
            url_kwargs.pop("host", None)
            url_kwargs.pop("port", None)
            url_kwargs.pop("ssl", None)
            _db = FalkorDB.from_url(url, **url_kwargs)
        else:
            _db = FalkorDB(**_connection_kwargs())
        graph_name = _setting("FALKORDB_GRAPH_NAME", "incident_investigator")
        _graph = _db.select_graph(graph_name)

    return _graph


def _research_graph() -> Any:
    """Select the research graph without changing the production graph name."""
    global _db
    if _db is None:
        _get_graph()
    return _db.select_graph("incident_rehearsal_research")


def get_murphy_faults() -> list[dict[str, Any]]:
    """Return recorded Murphy faults from the isolated research graph."""
    result = _research_graph().ro_query(
        """
        MATCH (run:ResearchRun)-[:HAS_FAULT]->(fault:FaultInjection)
        RETURN fault.id, fault.start_time, fault.end_time,
               fault.target_entity, fault.fault_category, fault.phase,
               run.id
        ORDER BY fault.start_time, fault.id
        """
    )
    return [
        {
            "id": _row_value(row, 0),
            "start_time": _row_value(row, 1),
            "end_time": _row_value(row, 2),
            "target_entity": _row_value(row, 3),
            "fault_category": _row_value(row, 4),
            "phase": _row_value(row, 5),
            "run_id": _row_value(row, 6),
        }
        for row in result.result_set
    ]


def get_murphy_fault_investigation(fault_id: str) -> dict[str, Any]:
    """Return fault, trace, span, parent, and mapped-service evidence."""
    graph = _research_graph()
    fault_result = graph.ro_query(
        """
        MATCH (run:ResearchRun)-[:HAS_FAULT]->(fault:FaultInjection)
        WHERE fault.id = $fault_id
        OPTIONAL MATCH (fault)-[:TARGETS]->(target:ResearchComponent)
        RETURN run.id, fault.id, fault.start_time, fault.end_time,
               fault.target_entity, fault.fault_category, fault.phase,
               target.id
        """,
        params={"fault_id": fault_id},
    )
    fault_rows = fault_result.result_set
    if not fault_rows:
        return {"fault": None, "spans": []}

    row = fault_rows[0]
    spans_result = graph.ro_query(
        """
        MATCH (run:ResearchRun)-[:HAS_FAULT]->(fault:FaultInjection)
        MATCH (run)-[:HAS_TRACE]->(trace:ResearchTrace)
              -[:HAS_SPAN]->(span:ResearchSpan)
        WHERE fault.id = $fault_id
        OPTIONAL MATCH (span)-[:MAPPED_TO_SERVICE]->(service:ResearchService)
        OPTIONAL MATCH (parent:ResearchSpan)-[:PARENT_OF]->(span)
        RETURN trace.id, span.id, span.original_span_id,
               span.parent_span_id, parent.original_span_id,
               span.process_id, span.service_name, service.name,
               span.operation_name, span.start_time,
               span.start_time_microseconds, span.duration_microseconds,
               span.overlaps_fault, span.source_file
        ORDER BY span.start_time, span.id
        """,
        params={"fault_id": fault_id},
    )
    return {
        "fault": {
            "run_id": _row_value(row, 0),
            "id": _row_value(row, 1),
            "start_time": _row_value(row, 2),
            "end_time": _row_value(row, 3),
            "target_entity": _row_value(row, 4),
            "fault_category": _row_value(row, 5),
            "phase": _row_value(row, 6),
            "target_component": _row_value(row, 7),
        },
        "spans": [
            {
                "trace_id": _row_value(span, 0),
                "span_id": _row_value(span, 1),
                "original_span_id": _row_value(span, 2),
                "parent_span_id": _row_value(span, 3),
                "resolved_parent_span_id": _row_value(span, 4),
                "process_id": _row_value(span, 5),
                "service_name": _row_value(span, 6),
                "mapped_service": _row_value(span, 7),
                "operation_name": _row_value(span, 8),
                "start_time": _row_value(span, 9),
                "start_time_microseconds": _row_value(span, 10),
                "duration_microseconds": _row_value(span, 11),
                "stored_overlaps_fault": _row_value(span, 12),
                "source_file": _row_value(span, 13),
            }
            for span in spans_result.result_set
        ],
    }


def _span_interval_seconds(
    span: dict[str, Any],
) -> tuple[float, float] | None:
    """Normalize a stored Jaeger span interval to Unix seconds."""
    start_microseconds = span.get("start_time_microseconds")
    duration_microseconds = span.get("duration_microseconds")
    start_seconds = span.get("start_time")
    if duration_microseconds is None:
        return None
    try:
        duration = float(duration_microseconds) / 1_000_000
        if start_microseconds is not None:
            start = float(start_microseconds) / 1_000_000
        elif start_seconds is not None:
            start = float(start_seconds)
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(start) or not math.isfinite(duration) or duration < 0:
        return None
    return start, start + duration


def _exact_fault_overlap(
    span: dict[str, Any],
    fault_start: Any,
    fault_end: Any,
) -> bool | str:
    """Compare one span with the selected fault, returning unknown if unsafe."""
    interval = _span_interval_seconds(span)
    try:
        start = float(fault_start)
        end = float(fault_end)
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if (
        interval is None
        or not math.isfinite(start)
        or not math.isfinite(end)
        or end < start
    ):
        return "unknown"
    span_start, span_end = interval
    return span_start < end and span_end > start


def analyze_murphy_fault(result: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, evidence-only analysis for one queried fault."""
    fault = result.get("fault")
    if not fault:
        return {
            "fault_duration_seconds": None,
            "overlapping_traces": [],
            "overlapping_spans": [],
            "breakdown": [],
            "parent_child_paths": [],
            "unresolved_parent_evidence": [],
            "sample_only_spans": [],
            "unknown_timestamp_spans": [],
            "conclusion": "The selected fault was not found in the research graph.",
        }

    try:
        fault_start = float(fault["start_time"])
        fault_end = float(fault["end_time"])
        duration = fault_end - fault_start
    except (KeyError, TypeError, ValueError, OverflowError):
        fault_start = fault_end = None
        duration = None

    overlapping: list[dict[str, Any]] = []
    sample_only: list[dict[str, Any]] = []
    unknown_timestamp: list[dict[str, Any]] = []
    for span in result.get("spans", []):
        overlap = (
            _exact_fault_overlap(span, fault_start, fault_end)
            if fault_start is not None
            else "unknown"
        )
        span["overlaps_selected_fault"] = overlap
        if overlap is True:
            overlapping.append(span)
        elif overlap == "unknown":
            unknown_timestamp.append(span)
        else:
            sample_only.append(span)

    breakdown_counter: Counter[tuple[str, str]] = Counter()
    for span in overlapping:
        service = span.get("mapped_service") or span.get("service_name") or "Unknown"
        operation = span.get("operation_name") or "Unknown"
        breakdown_counter[(service, operation)] += 1

    parent_child_paths = [
        {
            "trace_id": span.get("trace_id"),
            "parent_span_id": (
                span.get("resolved_parent_span_id")
                or span.get("parent_span_id")
                or "Unknown"
            ),
            "span_id": span.get("original_span_id"),
            "source_file": span.get("source_file") or "Unknown",
        }
        for span in overlapping
        if span.get("resolved_parent_span_id")
    ]
    unresolved_parent_evidence = [
        span.get("parent_span_id")
        for span in overlapping
        if span.get("parent_span_id") and not span.get("resolved_parent_span_id")
    ]
    trace_ids = sorted({str(span.get("trace_id")) for span in overlapping})
    span_ids = sorted({str(span.get("original_span_id")) for span in overlapping})

    if not overlapping:
        conclusion = (
            "No imported span overlaps the selected fault interval in the "
            "bounded research sample."
        )
    else:
        conclusion = (
            f"{len(overlapping)} imported span(s) across {len(trace_ids)} "
            "trace(s) overlap the selected fault interval. This is measured "
            "temporal overlap only and does not establish causality."
        )

    return {
        "fault_duration_seconds": duration,
        "overlapping_traces": trace_ids,
        "overlapping_spans": span_ids,
        "overlapping_span_records": overlapping,
        "breakdown": [
            {
                "Service mapping": service,
                "Operation": operation,
                "Overlapping spans": count,
            }
            for (service, operation), count in sorted(breakdown_counter.items())
        ],
        "parent_child_paths": parent_child_paths,
        "unresolved_parent_evidence": unresolved_parent_evidence,
        "sample_only_spans": sample_only,
        "unknown_timestamp_spans": unknown_timestamp,
        "conclusion": conclusion,
    }


def _safe_database_error(exc: Exception) -> str:
    """Describe a database failure without exposing connection details."""
    return (
        f"{type(exc).__name__}; check the FalkorDB endpoint, credentials, "
        "TLS settings, and availability."
    )


def _node_label(node: Any) -> str:
    """Return a display label from a FalkorDB node's properties."""
    properties = getattr(node, "properties", {}) or {}

    for property_name in ("name", "id", "title", "service_name"):
        value = properties.get(property_name)
        if value is not None:
            return str(value)

    return str(node)


def _format_path(path: Any) -> str:
    """Format a FalkorDB Path using its documented nodes() and edges() methods."""
    nodes = list(path.nodes())
    edges = list(path.edges())

    if not nodes:
        return str(path)

    parts: list[str] = [_node_label(nodes[0])]

    for index, edge in enumerate(edges):
        if index + 1 >= len(nodes):
            break

        parts.append(f"-[:{edge.relation}]->")
        parts.append(_node_label(nodes[index + 1]))

    return " ".join(parts)


def _row_value(row: Any, index: int) -> Any:
    """Read a positional value from a FalkorDB result_set row."""
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


def _run_read_query(query: str, service_name: str) -> Any:
    """Run a parameterized read-only query against the shared graph."""
    service_name = service_name.strip()

    return _get_graph().ro_query(
        query,
        params={"service_name": service_name},
    )


def get_service_graph_data() -> tuple[
    list[dict[str, str]], list[tuple[str, str]]
]:
    """Return service names, statuses, and directed dependency edges."""
    service_result = _get_graph().ro_query(
        """
        MATCH (service:Service)
        RETURN service.id, service.name, service.status
        ORDER BY service.name
        """
    )
    edge_result = _get_graph().ro_query(
        """
        MATCH (dependent:Service)-[:DEPENDS_ON]->(dependency:Service)
        RETURN dependent.name, dependency.name
        ORDER BY dependent.name, dependency.name
        """
    )

    services = [
        {
            "id": str(_row_value(row, 0) or ""),
            "name": str(_row_value(row, 1) or ""),
            "status": str(_row_value(row, 2) or ""),
        }
        for row in service_result.result_set
    ]
    dependencies = [
        (
            str(_row_value(row, 0) or ""),
            str(_row_value(row, 1) or ""),
        )
        for row in edge_result.result_set
    ]

    return services, dependencies


def _parse_timestamp(value: Any) -> datetime | None:
    """
    Parse a supported UTC timestamp.

    Accepts Unix timestamps in seconds and ISO 8601 timestamps. ISO values
    without an offset are treated as UTC. Invalid and out-of-range values
    return None instead of raising.
    """
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)

        timestamp = str(value).strip()
        if not timestamp:
            return None

        try:
            unix_timestamp = float(timestamp)
        except ValueError:
            unix_timestamp = None

        if unix_timestamp is not None:
            return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)

        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


@tool
def get_impacted_services(service_name: str) -> str:
    """
    Find services that directly or indirectly depend on a specified service.

    Use this tool when investigating the blast radius of a service failure or
    incident. It follows DEPENDS_ON relationships from dependent services to
    the target service for one to five hops and returns every matching path
    with its hop count.
    """
    query = """
    MATCH p = (dependent:Service)-[:DEPENDS_ON*1..5]->(target:Service)
    WHERE target.name = $service_name
    RETURN p, length(p) AS hops
    ORDER BY hops, dependent.name
    """

    try:
        result = _run_read_query(query, service_name)
        rows = result.result_set

        if not rows:
            return f"No impacted services were found for '{service_name}'."

        lines = [f"Impacted services for '{service_name}':"]
        for row in rows:
            path = _row_value(row, 0)
            hops = _row_value(row, 1)
            lines.append(f"- {_format_path(path)} ({hops} hop(s))")

        return "\n".join(lines)
    except Exception as exc:
        return (
            f"Unable to find impacted services for '{service_name}': "
            f"{_safe_database_error(exc)}"
        )


@tool
def get_recent_deployments(service_name: str) -> str:
    """
    Find deployments to a service from the last 24 hours.

    Use this tool when checking whether a recent deployment may be related to
    an incident. It reports version, commit hash, timestamp, description, and
    the exact Deployment-to-Service graph path. Missing or invalid timestamps
    are reported and are not classified as recent.
    """
    query = """
    MATCH p = (deployment:Deployment)-[:DEPLOYED_TO]->(service:Service)
    WHERE service.name = $service_name
    RETURN p,
           deployment.version,
           deployment.commit_hash,
           deployment.timestamp,
           deployment.description
    """

    try:
        result = _run_read_query(query, service_name)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        recent_deployments: list[str] = []
        invalid_timestamp_count = 0

        for row in result.result_set:
            path = _row_value(row, 0)
            version = _row_value(row, 1) or "unknown"
            commit_hash = _row_value(row, 2) or "unknown"
            timestamp_value = _row_value(row, 3)
            description = _row_value(row, 4) or "No description"

            timestamp = _parse_timestamp(timestamp_value)
            if timestamp is None:
                invalid_timestamp_count += 1
                continue

            if not cutoff <= timestamp <= now:
                continue

            recent_deployments.append(
                f"- {_format_path(path)}\n"
                f"  Version: {version}\n"
                f"  Commit: {commit_hash}\n"
                f"  Timestamp: {timestamp.isoformat()}\n"
                f"  Description: {description}"
            )

        if recent_deployments:
            lines = [
                f"Recent deployments for '{service_name}':",
                *recent_deployments,
            ]
            if invalid_timestamp_count:
                lines.append(
                    f"Skipped {invalid_timestamp_count} deployment(s) with "
                    "missing or invalid timestamps."
                )
            return "\n".join(lines)

        if invalid_timestamp_count:
            return (
                f"No recent deployments were found for '{service_name}'. "
                f"{invalid_timestamp_count} deployment(s) had missing or "
                "invalid timestamps and were skipped."
            )

        return f"No deployments from the last 24 hours were found for '{service_name}'."
    except Exception as exc:
        return (
            f"Unable to find recent deployments for '{service_name}': "
            f"{_safe_database_error(exc)}"
        )


@tool
def get_historical_incidents(service_name: str) -> str:
    """
    Find documented historical incidents that affected a service.

    Use this tool to compare a current incident with previous incidents. It
    follows PastIncident AFFECTED relationships, returns incident details,
    documented root causes, resolutions, and exact graph paths. Results are
    limited to a reasonable number.
    """
    query = f"""
    MATCH p = (incident:PastIncident)-[:AFFECTED]->(service:Service)
    WHERE service.name = $service_name
    RETURN p,
           incident.id,
           incident.title,
           incident.root_cause,
           incident.resolution
    LIMIT {_MAX_HISTORICAL_INCIDENTS}
    """

    try:
        result = _run_read_query(query, service_name)

        if not result.result_set:
            return f"No historical incidents were found for '{service_name}'."

        lines = [f"Historical incidents for '{service_name}':"]
        for row in result.result_set:
            path = _row_value(row, 0)
            incident_id = _row_value(row, 1) or "unknown"
            title = _row_value(row, 2) or "Untitled incident"
            root_cause = _row_value(row, 3) or "Not documented"
            resolution = _row_value(row, 4) or "Not documented"

            lines.append(
                f"- {_format_path(path)}\n"
                f"  Incident ID: {incident_id}\n"
                f"  Title: {title}\n"
                f"  Root cause: {root_cause}\n"
                f"  Resolution: {resolution}"
            )

        return "\n".join(lines)
    except Exception as exc:
        return (
            f"Unable to find historical incidents for '{service_name}': "
            f"{_safe_database_error(exc)}"
        )


@tool
def get_service_dependencies(service_name: str) -> str:
    """
    Find the direct dependencies of a service.

    Use this tool to determine what the selected service depends on by
    following outgoing DEPENDS_ON relationships. This is different from
    get_impacted_services, which finds services that may be affected by a
    failure in the selected service.
    """
    query = """
    MATCH p = (service:Service)-[:DEPENDS_ON]->(dependency:Service)
    WHERE service.name = $service_name
    RETURN p,
           service.name,
           dependency.name,
           dependency.status
    """

    try:
        result = _run_read_query(query, service_name)

        if not result.result_set:
            return (
                f"No direct dependencies were found by this query for "
                f"'{service_name}'."
            )

        lines = [f"Direct dependencies of '{service_name}':"]
        for row in result.result_set:
            path = _row_value(row, 0)
            requested_service = _row_value(row, 1) or service_name.strip()
            dependency_name = _row_value(row, 2) or "Unknown"
            dependency_status = _row_value(row, 3)
            status_text = (
                str(dependency_status)
                if dependency_status is not None
                else "Not recorded"
            )

            lines.append(
                f"- {requested_service} depends on {dependency_name}\n"
                f"  Path: {_format_path(path)}\n"
                f"  Status: {status_text}"
            )

        return "\n".join(lines)
    except Exception as exc:
        return (
            f"Unable to find direct dependencies for '{service_name}': "
            f"{_safe_database_error(exc)}"
        )


@tool
def get_service_blast_radius(service_name: str) -> str:
    """
    Measure the structural blast radius of a service within three dependency hops.

    Use this tool when assessing how many unique services may depend on the
    selected service. It counts unique dependent Service nodes, assigns each
    one its shortest dependency distance, and reports structural exposure only;
    it does not establish current outages, business criticality, or causality.
    """
    query = """
    MATCH (target:Service)
    WHERE target.name = $service_name
    OPTIONAL MATCH p = (dependent:Service)-[:DEPENDS_ON*1..3]->(target)
    RETURN target.name,
           dependent.id,
           dependent.name,
           p,
           CASE WHEN p IS NULL THEN NULL ELSE length(p) END
    ORDER BY dependent.name, length(p)
    """

    try:
        result = _run_read_query(query, service_name)
        rows = result.result_set

        if not rows:
            return (
                f"Service '{service_name.strip()}' was not found. "
                "No blast-radius measurements were made."
            )

        requested_name = _row_value(rows[0], 0) or service_name.strip()
        dependents: dict[str, dict[str, Any]] = {}

        for row in rows:
            dependent_id = _row_value(row, 1)
            dependent_name = _row_value(row, 2)
            path = _row_value(row, 3)
            hops = _row_value(row, 4)

            if dependent_name is None or hops is None:
                continue

            try:
                hop_count = int(hops)
            except (TypeError, ValueError):
                continue

            if hop_count < 1 or hop_count > 3:
                continue

            key = str(dependent_id or dependent_name)
            dependent = dependents.setdefault(
                key,
                {
                    "name": str(dependent_name),
                    "shortest_hops": hop_count,
                    "paths": [],
                },
            )

            if hop_count < dependent["shortest_hops"]:
                dependent["shortest_hops"] = hop_count
                dependent["paths"] = []

            if hop_count == dependent["shortest_hops"] and path is not None:
                formatted_path = _format_path(path)
                if formatted_path not in dependent["paths"]:
                    dependent["paths"].append(formatted_path)

        if not dependents:
            return (
                f"Service '{requested_name}' was found, but no dependent "
                "services were found within 3 hops by this query. This is a "
                "structural graph measurement, not evidence about outages or "
                "business criticality."
            )

        direct_count = sum(
            dependent["shortest_hops"] == 1
            for dependent in dependents.values()
        )
        indirect_count = sum(
            dependent["shortest_hops"] > 1
            for dependent in dependents.values()
        )
        maximum_distance = max(
            dependent["shortest_hops"] for dependent in dependents.values()
        )

        lines = [
            f"Structural blast radius for '{requested_name}' (up to 3 hops):",
            "Service found: yes",
            f"Unique dependent services: {len(dependents)}",
            f"Direct dependents (shortest distance 1): {direct_count}",
            f"Indirect dependents (shortest distance greater than 1): "
            f"{indirect_count}",
            f"Maximum shortest-path distance: {maximum_distance} hop(s)",
            "Dependent services and shortest dependency paths:",
        ]

        for dependent in sorted(
            dependents.values(),
            key=lambda item: (item["shortest_hops"], item["name"]),
        ):
            lines.append(
                f"- {dependent['name']} "
                f"({dependent['shortest_hops']} hop(s))"
            )
            for path in dependent["paths"]:
                lines.append(f"  Path: {path}")

        lines.append(
            "These counts describe structural graph exposure only; they do "
            "not confirm outages, business criticality, or root cause."
        )
        return "\n".join(lines)
    except Exception as exc:
        return (
            f"Unable to measure the service blast radius for "
            f"'{service_name}': {_safe_database_error(exc)}"
        )


@tool
def get_service_owner_and_runbook(service_name: str) -> str:
    """
    Find the maintainers and operational runbooks for a service.

    Use this tool when an investigator needs to contact the service owner or
    locate operational documentation. It separately follows MAINTAINED_BY and
    DOCUMENTED_IN relationships, returning all matching owners and runbooks
    with their exact graph paths.
    """
    owner_query = """
    MATCH p = (service:Service)-[:MAINTAINED_BY]->(developer:Developer)
    WHERE service.name = $service_name
    RETURN p,
           developer.name,
           developer.email,
           developer.on_call
    """

    runbook_query = """
    MATCH p = (service:Service)-[:DOCUMENTED_IN]->(runbook:Runbook)
    WHERE service.name = $service_name
    RETURN p,
           runbook.title,
           runbook.url
    """

    try:
        owner_result = _run_read_query(owner_query, service_name)
        runbook_result = _run_read_query(runbook_query, service_name)

        owners = [
            (
                f"- {_format_path(row[0])}\n"
                f"  Developer: {row[1] or 'Unknown'}\n"
                f"  Email: {row[2] or 'Unknown'}\n"
                f"  On-call: {row[3] if row[3] is not None else 'Unknown'}"
            )
            for row in owner_result.result_set[:_MAX_OWNER_AND_RUNBOOK_RESULTS]
        ]

        runbooks = [
            (
                f"- {_format_path(row[0])}\n"
                f"  Title: {row[1] or 'Untitled runbook'}\n"
                f"  URL: {row[2] or 'No URL documented'}"
            )
            for row in runbook_result.result_set[:_MAX_OWNER_AND_RUNBOOK_RESULTS]
        ]

        if not owners and not runbooks:
            return f"No owners or runbooks were found for '{service_name}'."

        lines = [f"Owners and runbooks for '{service_name}':"]

        lines.append("Owners:")
        lines.extend(owners or ["- None found."])

        lines.append("Runbooks:")
        lines.extend(runbooks or ["- None found."])

        return "\n".join(lines)
    except Exception as exc:
        return (
            f"Unable to find owners or runbooks for '{service_name}': "
            f"{_safe_database_error(exc)}"
        )


incident_tools = [
    get_impacted_services,
    get_service_dependencies,
    get_service_blast_radius,
    get_recent_deployments,
    get_historical_incidents,
    get_service_owner_and_runbook,
]
