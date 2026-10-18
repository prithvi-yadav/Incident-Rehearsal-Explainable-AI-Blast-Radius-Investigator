"""Streamlit interface for the AI Incident Investigator."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import streamlit as st

from agent import investigate_incident
from tools import (
    analyze_murphy_fault,
    get_murphy_fault_investigation,
    get_murphy_faults,
    get_service_graph_data,
)


logger = logging.getLogger(__name__)


DEFAULT_INCIDENT = (
    "AuthService is failing and customers are reporting Checkout timeouts."
)

STATUS_COLORS = {
    "healthy": "#2e7d32",
    "degraded": "#d97706",
    "failing": "#c62828",
}
UNKNOWN_STATUS_COLOR = "#6b7280"
BASELINE_SCENARIO = "Baseline — no hypothetical change"


def _dot_escape(value: str) -> str:
    """Escape text safely for a quoted Graphviz DOT string."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _build_dependency_graph(
    services: list[dict[str, str]],
    dependencies: list[tuple[str, str]],
    focus_service: str,
    reachable_services: set[str] | None = None,
    no_longer_reachable: set[str] | None = None,
) -> str:
    """Build a left-to-right Graphviz graph from graph records."""
    reachable_services = reachable_services or set()
    no_longer_reachable = no_longer_reachable or set()
    lines = [
        "digraph service_dependencies {",
        '  graph [rankdir=LR, bgcolor="transparent", pad="0.45", '
        'nodesep="0.8", ranksep="1.25", margin="0.2"];',
        '  node [shape=box, style="rounded,filled", fontname="Arial", '
        'fontsize="13", fontcolor="#f8fafc", margin="0.22,0.16", '
        'width="1.8", height="0.7"];',
        '  edge [color="#94a3b8", penwidth="1.6", arrowsize="0.8", '
        'fontname="Arial", fontsize="10"];',
    ]

    for service in services:
        name = service.get("name", "").strip()
        if not name:
            continue
        status = service.get("status", "").strip().lower()
        color = STATUS_COLORS.get(status, UNKNOWN_STATUS_COLOR)
        if name == focus_service:
            border = "#38bdf8"
            penwidth = "3.5"
        elif name in no_longer_reachable:
            border = "#f97316"
            penwidth = "3.0"
        elif name in reachable_services:
            border = "#86efac"
            penwidth = "2.5"
        else:
            border = "#475569"
            penwidth = "1.5"
        status_label = status or "status not recorded"
        label = f"{name}\n{status_label}"
        lines.append(
            f'  "{_dot_escape(name)}" [label="{_dot_escape(label)}", '
            f'fillcolor="{color}", color="{border}", penwidth="{penwidth}"];'
        )

    for dependent, dependency in dependencies:
        dependent = dependent.strip()
        dependency = dependency.strip()
        if not dependent or not dependency:
            continue
        lines.append(
            f'  "{_dot_escape(dependent)}" -> "{_dot_escape(dependency)}";'
        )

    lines.append("}")
    return "\n".join(lines)


def _service_mentioned_in_incident(
    incident_description: str,
    service_names: list[str],
) -> str | None:
    """Return the first graph service name mentioned in the incident text."""
    description = incident_description.casefold()
    for service_name in service_names:
        if service_name.casefold() in description:
            return service_name
    return None


def _calculate_blast_radius(
    service_name: str,
    dependencies: list[tuple[str, str]],
    service_names: list[str] | None = None,
    max_hops: int = 3,
) -> dict[str, Any]:
    """Calculate unique incoming dependents and their shortest distances."""
    incoming: dict[str, set[str]] = {}
    for dependent, dependency in dependencies:
        dependent = dependent.strip()
        dependency = dependency.strip()
        if dependent and dependency:
            incoming.setdefault(dependency, set()).add(dependent)

    distances: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque([(service_name, 0)])
    while queue:
        current, distance = queue.popleft()
        if distance >= max_hops:
            continue
        for dependent in sorted(incoming.get(current, set())):
            next_distance = distance + 1
            if dependent not in distances or next_distance < distances[dependent]:
                distances[dependent] = next_distance
                queue.append((dependent, next_distance))

    direct = sum(distance == 1 for distance in distances.values())
    indirect = sum(distance > 1 for distance in distances.values())
    known_services = set(service_names or ())
    known_services.update(
        name
        for edge in dependencies
        for name in edge
    )
    return {
        "service_found": service_name in known_services,
        "dependents": distances,
        "unique_count": len(distances),
        "direct_count": direct,
        "indirect_count": indirect,
        "maximum_distance": max(distances.values(), default=0),
    }


def _compare_blast_radius(
    baseline: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, list[str]]:
    """Compare dependent reachability between two graph scenarios."""
    baseline_names = set(baseline["dependents"])
    scenario_names = set(scenario["dependents"])
    return {
        "no_longer_reachable": sorted(baseline_names - scenario_names),
        "remain_affected": sorted(baseline_names & scenario_names),
        "newly_reachable": sorted(scenario_names - baseline_names),
    }


def _build_isolation_result(
    focus_service: str,
    dependencies: list[tuple[str, str]],
    service_names: list[str],
    isolated_edge: tuple[str, str],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Calculate one deterministic in-memory edge-isolation scenario."""
    simulated_dependencies = [
        edge for edge in dependencies if edge != isolated_edge
    ]
    simulated = _calculate_blast_radius(
        focus_service,
        simulated_dependencies,
        service_names,
    )
    comparison = _compare_blast_radius(baseline, simulated)
    return {
        "focus_service": focus_service,
        "scenario": (
            f"Hypothetically isolate: "
            f"{isolated_edge[0]} → {isolated_edge[1]}"
        ),
        "isolated_edge": isolated_edge,
        "baseline": baseline,
        "simulated": simulated,
        "comparison": comparison,
        "lost_count": len(comparison["no_longer_reachable"]),
    }


def _format_service_names(names: list[str]) -> str:
    """Format a service-name list for a compact UI summary."""
    return ", ".join(names) if names else "None"


def _report_summary(report: str) -> str:
    """Extract the incident-summary section when the report provides one."""
    lines = report.splitlines()
    in_summary = False
    summary: list[str] = []
    for line in lines:
        if line.strip().lower() in {"## incident summary", "### incident summary"}:
            in_summary = True
            continue
        if in_summary and line.startswith("#"):
            break
        if in_summary and line.strip():
            summary.append(line.strip())
    return " ".join(summary) or "A complete investigation report is available."


def main() -> None:
    """Render the incident investigation interface."""
    st.set_page_config(
        page_title="AI Incident Investigator",
        page_icon="🔎",
        layout="wide",
    )

    st.markdown(
        """
        <style>
        .block-container {
            max-width: 1440px;
            padding-top: 1.75rem;
            padding-bottom: 2rem;
        }
        .app-subtitle {
            color: #5f6b7a;
            font-size: 1rem;
            line-height: 1.6;
            margin-bottom: 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("AI Incident Investigator")
    st.markdown(
        (
            '<p class="app-subtitle">'
            "Investigate software incidents by tracing service dependencies and "
            "retrieving deployment history, past incidents, service owners, "
            "and runbooks from FalkorDB."
            "</p>"
        ),
        unsafe_allow_html=True,
    )

    try:
        services, dependencies = get_service_graph_data()
        graph_error = None
    except Exception as exc:
        logger.exception("Unable to load service graph data")
        graph_error = exc
        services, dependencies = [], []

    service_names = [service["name"] for service in services if service["name"]]
    overview_tab, rehearsal_tab, report_tab = st.tabs(
        ["Overview", "Incident Rehearsal", "Investigation Report"]
    )

    with overview_tab:
        incident_description = st.text_area(
            "Describe the incident",
            value=DEFAULT_INCIDENT,
            height=110,
            placeholder="For example: Checkout is timing out after an AuthService failure.",
            help="Include the affected service and any observed customer impact.",
        )

        focus_service = ""
        if service_names:
            mentioned_service = _service_mentioned_in_incident(
                incident_description,
                service_names,
            )
            previous_description = st.session_state.get("_incident_description")
            if previous_description != incident_description:
                st.session_state["focus_service"] = (
                    mentioned_service or service_names[0]
                )
                st.session_state["_incident_description"] = incident_description
            elif st.session_state.get("focus_service") not in service_names:
                st.session_state["focus_service"] = service_names[0]

            focus_service = st.selectbox(
                "Focus service",
                options=service_names,
                key="focus_service",
                help="Highlight a service in the live dependency graph.",
            )
        elif graph_error is not None:
            st.warning(
                "The service graph is currently unavailable. "
                "Check that FalkorDB is running and try again."
            )
        else:
            st.info("No service nodes are available to visualize yet.")

        focused_record = next(
            (
                service
                for service in services
                if service.get("name") == focus_service
            ),
            {},
        )
        blast_radius = _calculate_blast_radius(
            focus_service,
            dependencies,
            service_names,
        ) if focus_service else {
            "unique_count": 0,
            "direct_count": 0,
            "indirect_count": 0,
            "maximum_distance": 0,
        }
        kpi_columns = st.columns(4)
        kpi_values = (
            ("Services", len(services)),
            ("Relationships", len(dependencies)),
            ("Focused status", focused_record.get("status") or "unknown"),
            ("Unique dependents", blast_radius["unique_count"]),
        )
        for column, (label, value) in zip(kpi_columns, kpi_values):
            column.metric(label, value, border=True)

        if focus_service:
            st.subheader("Service dependency graph")
            st.caption(
                "A → B means service A depends on service B. "
                "Structural dependencies do not prove a real-time outage or "
                "root cause."
            )
            st.graphviz_chart(
                _build_dependency_graph(services, dependencies, focus_service),
                width="stretch",
                height=610,
            )
            st.caption(
                "Legend: "
                ":green[healthy] · :orange[degraded] · :red[failing] · "
                ":gray[unknown]. Blue border indicates the focused service."
            )

        if st.button(
            "Investigate Incident",
            type="primary",
            width="stretch",
        ):
            if not incident_description.strip():
                st.warning(
                    "Please describe an incident before starting an investigation."
                )
            elif not focus_service:
                st.warning("Select a focus service before starting an investigation.")
            else:
                with st.spinner(
                    "Investigating the incident using FalkorDB evidence..."
                ):
                    try:
                        report = investigate_incident(
                            incident_description,
                            service_name=focus_service,
                        )
                    except Exception:
                        logger.exception("Incident investigation failed")
                        st.error(
                            "The investigation could not be completed. "
                            "Check that FalkorDB is running and the Gemini API "
                            "configuration is available, then try again."
                        )
                    else:
                        st.session_state["investigation_report"] = report
                        st.success(
                            "Investigation complete. Open the Investigation "
                            "Report tab to review the findings."
                        )

    with rehearsal_tab:
        st.subheader("Incident Rehearsal")
        st.caption(
            "Simulate isolating one actual incoming dependency edge without "
            "changing FalkorDB. This changes graph reachability only; it does "
            "not prove a production fix is safe, preserve functionality, or "
            "confirm a real-world outage."
        )
        if focus_service:
            incoming_edges = sorted(
                {
                    (dependent.strip(), dependency.strip())
                    for dependent, dependency in dependencies
                    if dependent.strip() and dependency.strip() == focus_service
                }
            )
            scenario_options = [BASELINE_SCENARIO] + [
                f"Hypothetically isolate: {dependent} → {dependency}"
                for dependent, dependency in incoming_edges
            ]
            scenario_edges = {
                option: edge
                for option, edge in zip(scenario_options[1:], incoming_edges)
            }
            scenario = st.selectbox(
                "Rehearsal scenario",
                options=scenario_options,
                key="rehearsal_scenario",
            )

            baseline = _calculate_blast_radius(
                focus_service,
                dependencies,
                service_names,
            )

            if st.button("Run Rehearsal", width="stretch"):
                isolated_edge = scenario_edges.get(scenario)
                if isolated_edge is None:
                    simulated = baseline
                    comparison = _compare_blast_radius(baseline, simulated)
                    st.session_state["rehearsal_result"] = {
                        "focus_service": focus_service,
                        "scenario": scenario,
                        "isolated_edge": None,
                        "baseline": baseline,
                        "simulated": simulated,
                        "comparison": comparison,
                        "lost_count": 0,
                    }
                else:
                    st.session_state["rehearsal_result"] = (
                        _build_isolation_result(
                            focus_service,
                            dependencies,
                            service_names,
                            isolated_edge,
                            baseline,
                        )
                    )

            rehearsal = st.session_state.get("rehearsal_result")
            st.markdown("**Automatic Scenario Analyzer**")
            st.caption(
                "Compare every actual incoming edge as a graph-only hypothetical "
                "scenario. This ranking is not a production recommendation or "
                "proof that an action is safe."
            )
            if st.button("Compare All Scenarios", width="stretch"):
                all_results = [
                    _build_isolation_result(
                        focus_service,
                        dependencies,
                        service_names,
                        edge,
                        baseline,
                    )
                    for edge in incoming_edges
                ]
                all_results.sort(
                    key=lambda result: (
                        -result["lost_count"],
                        result["scenario"],
                    )
                )
                st.session_state["all_rehearsal_results"] = {
                    "focus_service": focus_service,
                    "results": all_results,
                }

            all_rehearsal = st.session_state.get("all_rehearsal_results")
            automatic_result = None
            if (
                all_rehearsal
                and all_rehearsal["focus_service"] == focus_service
                and all_rehearsal["results"]
            ):
                results = all_rehearsal["results"]
                rows = [
                    {
                        "Rank": index,
                        "Hypothetically isolated edge": (
                            f"{result['isolated_edge'][0]} → "
                            f"{result['isolated_edge'][1]}"
                        ),
                        "Unique dependents before": result["baseline"][
                            "unique_count"
                        ],
                        "Unique dependents after": result["simulated"][
                            "unique_count"
                        ],
                        "Become unreachable": result["lost_count"],
                        "Direct after": result["simulated"]["direct_count"],
                        "Indirect after": result["simulated"]["indirect_count"],
                        "Services no longer reachable": _format_service_names(
                            result["comparison"]["no_longer_reachable"]
                        ),
                    }
                    for index, result in enumerate(results, start=1)
                ]
                st.table(rows)
                automatic_options = [
                    result["scenario"] for result in results
                ]
                automatic_scenario = st.selectbox(
                    "View a comparison result",
                    options=automatic_options,
                    key="automatic_rehearsal_scenario",
                )
                automatic_result = next(
                    result
                    for result in results
                    if result["scenario"] == automatic_scenario
                )
            elif all_rehearsal and all_rehearsal["focus_service"] == focus_service:
                st.info(
                    "No incoming dependency scenarios are available for this "
                    "focus service."
                )

            if automatic_result is not None:
                display_rehearsal = automatic_result
            elif rehearsal and rehearsal["focus_service"] == focus_service:
                display_rehearsal = rehearsal
            else:
                display_rehearsal = None

            if (
                display_rehearsal
                and (
                    automatic_result is not None
                    or display_rehearsal["scenario"] == scenario
                )
            ):
                baseline = display_rehearsal["baseline"]
                simulated = display_rehearsal["simulated"]
                comparison = display_rehearsal["comparison"]
                st.markdown(
                    f"**{display_rehearsal['scenario']}**  \n"
                    "The comparison uses current graph data and an in-memory "
                    "copy of its edges."
                )
                isolated_edge = display_rehearsal["isolated_edge"]
                if isolated_edge is not None:
                    st.markdown(
                        "**Hypothetically isolated edge:** "
                        f"{isolated_edge[0]} → {isolated_edge[1]}"
                    )
                else:
                    st.markdown("**Hypothetically isolated edge:** None")

                before_column, after_column = st.columns(2)
                graph_columns = (
                    (before_column, "Before — Current Graph", baseline, dependencies),
                    (
                        after_column,
                        "After — Simulated Graph",
                        simulated,
                        [
                            edge
                            for edge in dependencies
                            if edge != isolated_edge
                        ],
                    ),
                )
                for column, heading, values, graph_edges in graph_columns:
                    with column:
                        st.markdown(f"**{heading}**")
                        st.graphviz_chart(
                            _build_dependency_graph(
                                services,
                                graph_edges,
                                focus_service,
                                reachable_services=set(values["dependents"]),
                                no_longer_reachable=set(
                                    comparison["no_longer_reachable"]
                                ),
                            ),
                            width="stretch",
                            height=360,
                        )
                        for label, key in (
                            ("Unique dependents", "unique_count"),
                            ("Direct dependents", "direct_count"),
                            ("Indirect dependents", "indirect_count"),
                            ("Maximum shortest distance", "maximum_distance"),
                        ):
                            st.metric(label, values[key], border=True)

                st.caption(
                    "Legend: "
                    ":green[healthy] · :orange[degraded] · :red[failing] · "
                    ":gray[unknown]. Blue border = selected service; green "
                    "border = reachable dependent; orange border = no longer "
                    "reachable after isolation."
                )
                st.markdown(
                    "**No longer reachable:** "
                    f"{_format_service_names(comparison['no_longer_reachable'])}"
                )
                st.markdown(
                    "**Remain affected:** "
                    f"{_format_service_names(comparison['remain_affected'])}"
                )
                st.markdown(
                    "**Newly reachable:** "
                    f"{_format_service_names(comparison['newly_reachable'])}"
                )
                st.caption(
                    "Path impact: paths to "
                    f"{_format_service_names(comparison['no_longer_reachable'])} "
                    "are removed in the simulated graph; paths remain for "
                    f"{_format_service_names(comparison['remain_affected'])}."
                )
                if comparison["no_longer_reachable"]:
                    st.info(
                        "This graph-only rehearsal removes reachability for the "
                        "listed services. It is not a production safety or "
                        "outage conclusion."
                    )
                else:
                    st.info(
                        "This scenario did not change dependent-service "
                        "reachability in the current graph."
                    )
            elif rehearsal and rehearsal["focus_service"] == focus_service:
                st.warning(
                    f"Showing no results for '{scenario}' yet. The last "
                    f"completed rehearsal was '{rehearsal['scenario']}'. "
                    "Click Run Rehearsal to calculate the selected scenario."
                )
        else:
            st.info("Select a focus service in Overview to configure a rehearsal.")

    with report_tab:
        st.subheader("Investigation Report")
        report = st.session_state.get("investigation_report")
        if report:
            st.info(_report_summary(report), icon=":material/summarize:")
            st.download_button(
                "Download Markdown Report",
                data=report,
                file_name="incident-investigation-report.md",
                mime="text/markdown",
                width="content",
            )
            st.markdown(report)
        else:
            st.info(
                "Run an investigation from the Overview tab to generate a report."
            )

    with st.expander("Murphy Fault Investigator", expanded=False):
        st.subheader("Murphy Fault Investigator")
        st.caption(
            "Explore bounded research evidence from the isolated "
            "`incident_rehearsal_research` graph. These are experimental "
            "DeathStarBench observations with injected faults, not production "
            "incidents."
        )
        if st.button("Load recorded Murphy faults", width="stretch"):
            try:
                st.session_state["murphy_faults"] = get_murphy_faults()
                st.session_state.pop("murphy_fault_result", None)
            except Exception:
                logger.exception("Unable to load Murphy research faults")
                st.error(
                    "The Murphy research graph could not be queried. "
                    "Check that the isolated research graph is available."
                )

        murphy_faults = st.session_state.get("murphy_faults", [])
        if murphy_faults:
            fault_options = {
                (
                    f"{fault['phase']} · {fault['target_entity']} · "
                    f"{fault['start_time']}–{fault['end_time']}"
                ): fault
                for fault in murphy_faults
            }
            selected_fault_label = st.selectbox(
                "Recorded fault",
                options=list(fault_options),
                key="murphy_fault_selection",
            )
            selected_fault = fault_options[selected_fault_label]
            if st.button("Investigate recorded fault", width="stretch"):
                try:
                    st.session_state["murphy_fault_result"] = (
                        get_murphy_fault_investigation(selected_fault["id"])
                    )
                except Exception:
                    logger.exception("Unable to investigate Murphy fault")
                    st.error(
                        "The recorded fault could not be investigated from "
                        "the research graph."
                    )

            result = st.session_state.get("murphy_fault_result")
            if result and (result.get("fault") or {}).get("id") == selected_fault["id"]:
                fault = result["fault"]
                analysis = analyze_murphy_fault(result)
                duration = analysis["fault_duration_seconds"]
                duration_text = (
                    f"{duration:g} seconds"
                    if duration is not None
                    else "unknown"
                )
                st.markdown(
                    f"**Fault target:** `{fault['target_entity']}`  \n"
                    f"**Interval:** `{fault['start_time']}` – "
                    f"`{fault['end_time']}` Unix seconds  \n"
                    f"**Category:** `{fault['fault_category']}`  \n"
                    f"**Phase:** `{fault['phase']}`  \n"
                    f"**Duration:** `{duration_text}`"
                )
                st.markdown("**Evidence-based investigation summary**")
                summary_columns = st.columns(3)
                summary_columns[0].metric(
                    "Overlapping traces",
                    len(analysis["overlapping_traces"]),
                )
                summary_columns[1].metric(
                    "Overlapping spans",
                    len(analysis["overlapping_spans"]),
                )
                summary_columns[2].metric(
                    "Bounded sample spans",
                    len(result.get("spans", [])),
                )
                st.info(analysis["conclusion"])
                if analysis["breakdown"]:
                    st.markdown("**Service and operation breakdown**")
                    st.table(analysis["breakdown"])
                if analysis["overlapping_spans"]:
                    st.markdown(
                        "**Supporting trace evidence**  \n"
                        f"Trace IDs: `{', '.join(analysis['overlapping_traces'])}`  \n"
                        f"Span IDs: `{', '.join(analysis['overlapping_spans'])}`"
                    )
                if analysis["parent_child_paths"]:
                    st.markdown("**Explicit parent-child trace paths**")
                    st.table(
                        [
                            {
                                "Trace": path["trace_id"],
                                "Parent span": path["parent_span_id"],
                                "Overlapping span": path["span_id"],
                                "Source": path["source_file"],
                            }
                            for path in analysis["parent_child_paths"]
                        ]
                    )
                if analysis["unresolved_parent_evidence"]:
                    st.caption(
                        f"{len(analysis['unresolved_parent_evidence'])} "
                        "overlapping span(s) reference a parent span that was "
                        "not present in the bounded imported sample; no path "
                        "was inferred."
                    )
                if analysis["sample_only_spans"] or analysis["unknown_timestamp_spans"]:
                    st.markdown("**Evidence boundary**")
                    st.caption(
                        f"{len(analysis['sample_only_spans'])} span(s) are in "
                        "the bounded sample but do not overlap this exact fault; "
                        f"{len(analysis['unknown_timestamp_spans'])} span(s) "
                        "have missing or ambiguous timestamps."
                    )
                st.markdown(
                    "**Evidence versus inference**  \n"
                    "Measured evidence includes the selected fault interval, "
                    "Jaeger span timestamps, operation names, process service "
                    "mappings, source files, and explicit parent-child "
                    "references. The bounded sample does not establish "
                    "production impact, service causality, baseline behavior, "
                    "CPU or memory effects, ownership, deployments, or "
                    "runbooks. Temporal overlap alone does not prove that the "
                    "injected fault caused any span behavior."
                )
                spans = result.get("spans", [])
                if not spans:
                    st.warning(
                        "No suitable traces or spans were found in the bounded "
                        "research sample for this fault."
                    )
                else:
                    st.markdown("**Measured trace evidence**")
                    st.table(
                        [
                            {
                                "Trace": span["trace_id"],
                                "Span": span["original_span_id"],
                                "Parent span": (
                                    span["resolved_parent_span_id"]
                                    or span["parent_span_id"]
                                    or "Not resolved"
                                ),
                                "Service mapping": (
                                    span["mapped_service"]
                                    or span["service_name"]
                                    or "Unknown"
                                ),
                                "Operation": span["operation_name"] or "Unknown",
                                "Overlaps selected fault": span.get(
                                    "overlaps_selected_fault", "unknown"
                                ),
                                "Source": span["source_file"],
                            }
                            for span in spans
                        ]
                    )
                    st.info(
                        "Temporal overlap is a measured relationship between "
                        "recorded intervals. It does not prove that the injected "
                        "fault caused the observed span behavior, nor that this "
                        "benchmark represents a production outage."
                    )
                    st.markdown(
                        "**Evidence boundary:** Parent-child references and "
                        "Jaeger process mappings are recorded trace facts. "
                        "Production ownership, deployments, runbooks, and "
                        "causal conclusions are unknown from this dataset."
                    )
        else:
            st.info(
                "Load the recorded Murphy faults to query the isolated research "
                "graph. No database writes or Gemini calls are made."
            )


if __name__ == "__main__":
    main()
