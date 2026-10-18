"""Bounded, dry-run-first importer for the Murphy compute-fault traces."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tarfile
from typing import Any, BinaryIO, Iterator

from dotenv import load_dotenv
from falkordb import FalkorDB


load_dotenv()

DATASET_ID = "IDB-6641912"
DEFAULT_GRAPH_NAME = "incident_rehearsal_research"
PRODUCTION_GRAPH_NAME = "incident_investigator"
DEFAULT_RUN_ID = "compute-12"
DEFAULT_MAX_TRACES = 5
DEFAULT_MAX_SPANS = 100
DEFAULT_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class FaultRecord:
    fault_id: str
    start_time: int
    end_time: int
    target_entity: str
    fail_param: str
    phase: str
    fault_category: str = "compute"


def _streamlit_secret(name: str) -> str | None:
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except (FileNotFoundError, KeyError, RuntimeError):
        return None
    return str(value).strip() if value is not None else None


def _setting(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name) or _streamlit_secret(name)
    return value.strip() if value else default


def _setting_bool(name: str, default: bool = False) -> bool:
    value = _setting(name)
    return default if value is None else value.casefold() in {"1", "true", "yes", "on"}


def _connection_kwargs() -> dict[str, Any]:
    port_text = _setting("FALKORDB_PORT", "6379") or "6379"
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("FALKORDB_PORT must be a valid integer.") from exc

    kwargs: dict[str, Any] = {
        "host": _setting("FALKORDB_HOST", "localhost"),
        "port": port,
        "ssl": _setting_bool("FALKORDB_SSL"),
    }
    for option, variable in {
        "username": "FALKORDB_USERNAME",
        "password": "FALKORDB_PASSWORD",
        "ssl_cert_reqs": "FALKORDB_SSL_CERT_REQS",
        "ssl_ca_certs": "FALKORDB_SSL_CA_CERTS",
        "ssl_ca_path": "FALKORDB_SSL_CA_PATH",
        "ssl_ca_data": "FALKORDB_SSL_CA_DATA",
        "ssl_certfile": "FALKORDB_SSL_CERTFILE",
        "ssl_keyfile": "FALKORDB_SSL_KEYFILE",
    }.items():
        value = _setting(variable)
        if value is not None:
            kwargs[option] = value
    if _setting("FALKORDB_SSL_CHECK_HOSTNAME") is not None:
        kwargs["ssl_check_hostname"] = _setting_bool(
            "FALKORDB_SSL_CHECK_HOSTNAME"
        )
    return kwargs


def _create_database() -> FalkorDB:
    url = _setting("FALKORDB_URL")
    if url:
        kwargs = _connection_kwargs()
        for key in ("host", "port", "ssl"):
            kwargs.pop(key, None)
        return FalkorDB.from_url(url, **kwargs)
    return FalkorDB(**_connection_kwargs())


def _stable_id(*parts: object) -> str:
    return "|".join(str(part).replace("|", "%7C") for part in parts)


def _parse_fault_lines(text: str, phase: str) -> list[FaultRecord]:
    records: list[FaultRecord] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4:
            raise ValueError(
                f"{phase}: line {line_number} must contain four fields."
            )
        try:
            start_time = int(fields[0])
            end_time = int(fields[1])
        except ValueError as exc:
            raise ValueError(
                f"{phase}: line {line_number} has invalid timestamps."
            ) from exc
        if end_time < start_time:
            raise ValueError(f"{phase}: line {line_number} ends before it starts.")
        target, fail_param = fields[2], fields[3]
        records.append(
            FaultRecord(
                fault_id=_stable_id(DATASET_ID, phase, line_number),
                start_time=start_time,
                end_time=end_time,
                target_entity=target,
                fail_param=fail_param,
                phase=phase,
            )
        )
    return records


def _parse_collection_times(text: str) -> tuple[int, int]:
    fields = text.split()
    if len(fields) != 2:
        raise ValueError("start_end_times must contain two Unix timestamps.")
    try:
        start_time, end_time = (int(field) for field in fields)
    except ValueError as exc:
        raise ValueError("start_end_times contains an invalid timestamp.") from exc
    if end_time < start_time:
        raise ValueError("start_end_times ends before it starts.")
    return start_time, end_time


def _iter_json_array_items(
    stream: BinaryIO,
    array_key: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[Any]:
    """Yield JSON array items incrementally without loading the array."""
    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    eof = False

    def fill() -> None:
        nonlocal buffer, position, eof
        chunk = stream.read(chunk_size)
        if chunk:
            buffer = buffer[position:] + chunk.decode("utf-8", errors="replace")
            position = 0
        else:
            buffer = buffer[position:]
            position = 0
            eof = True

    while not eof and len(buffer) < chunk_size:
        fill()
        if eof:
            break

    if array_key is None:
        while position < len(buffer) and buffer[position].isspace():
            position += 1
        if position >= len(buffer) or buffer[position] != "[":
            raise ValueError("Expected a top-level JSON array.")
        position += 1
    else:
        marker = f'"{array_key}"'
        while True:
            index = buffer.find(marker, position)
            if index >= 0:
                colon = buffer.find(":", index + len(marker))
                if colon >= 0:
                    position = colon + 1
                    break
            if eof:
                raise ValueError(f"JSON array key {array_key!r} was not found.")
            fill()

        while position < len(buffer) and buffer[position].isspace():
            position += 1
        if position >= len(buffer) or buffer[position] != "[":
            raise ValueError(f"JSON key {array_key!r} is not an array.")
        position += 1

    while True:
        while position < len(buffer) and buffer[position].isspace():
            position += 1
        if position >= len(buffer):
            if eof:
                return
            fill()
            continue
        if buffer[position] == "]":
            return
        if buffer[position] == ",":
            position += 1
            continue
        try:
            item, end = decoder.raw_decode(buffer, position)
        except json.JSONDecodeError:
            if eof:
                raise ValueError("Incomplete JSON item at end of stream.")
            fill()
            continue
        yield item
        position = end


def _span_interval_microseconds(span: dict[str, Any]) -> tuple[int, int]:
    start_microseconds = int(span["startTime"])
    duration_microseconds = int(span.get("duration", 0))
    return (
        start_microseconds,
        start_microseconds + duration_microseconds,
    )


def _overlaps(
    span: dict[str, Any],
    fault_start: int,
    fault_end: int,
) -> bool:
    span_start, span_end = _span_interval_microseconds(span)
    fault_start_microseconds = fault_start * 1_000_000
    fault_end_microseconds = fault_end * 1_000_000
    return (
        span_start < fault_end_microseconds
        and span_end > fault_start_microseconds
    )


def _process_service_name(
    trace: dict[str, Any],
    process_id: str | None,
) -> str | None:
    if not process_id:
        return None
    process = (trace.get("processes") or {}).get(process_id)
    if not isinstance(process, dict):
        return None
    value = process.get("serviceName")
    return str(value) if value else None


def _select_trace_records(
    trace: dict[str, Any],
    faults: list[FaultRecord],
    selected_trace_count: int,
    max_spans: int,
    source_file: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    spans = trace.get("spans")
    if not isinstance(spans, list) or not trace.get("traceID"):
        return None
    fault_start = min(fault.start_time for fault in faults)
    fault_end = max(fault.end_time for fault in faults)
    relevant = [
        span for span in spans
        if isinstance(span, dict) and _overlaps(span, fault_start, fault_end)
    ]
    if not relevant:
        return None
    selected = relevant[:max_spans]
    trace_copy = {
        "trace_id": str(trace["traceID"]),
        "source_file": source_file,
        "span_count": len(spans),
        "service_names": sorted(
            {
                service_name
                for span in selected
                if (service_name := _process_service_name(
                    trace, span.get("processID")
                ))
            }
        ),
        "selected_trace_number": selected_trace_count + 1,
    }
    return trace_copy, selected


def inspect_archive(
    archive_path: Path,
    max_traces: int = DEFAULT_MAX_TRACES,
    max_spans: int = DEFAULT_MAX_SPANS,
) -> dict[str, Any]:
    """Parse bounded metadata and relevant spans from one archive."""
    if max_traces < 1 or max_spans < 1:
        raise ValueError("max_traces and max_spans must be positive.")

    metadata: dict[str, str] = {}
    selected_traces: list[dict[str, Any]] = []
    selected_spans: list[dict[str, Any]] = []
    seen_span_ids: set[str] = set()

    try:
        with tarfile.open(archive_path, "r|gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = member.name.replace("\\", "/")
                basename = Path(name).name
                if basename in {"start_end_times", "interfere", "train_interfere"}:
                    stream = archive.extractfile(member)
                    if stream is not None:
                        metadata[basename] = stream.read(16_384).decode(
                            "utf-8", errors="replace"
                        )
                    continue
                if not name.endswith("jaeger_traces/traces1.json"):
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                _parse_collection_times(metadata["start_end_times"])
                faults = _parse_fault_lines(metadata["interfere"], "interfere")
                faults += _parse_fault_lines(
                    metadata["train_interfere"], "train_interfere"
                )
                for trace in _iter_json_array_items(stream, array_key="data"):
                    if len(selected_traces) >= max_traces:
                        break
                    if not isinstance(trace, dict):
                        continue
                    selected = _select_trace_records(
                        trace,
                        faults,
                        len(selected_traces),
                        max_spans,
                        name,
                    )
                    if selected is None:
                        continue
                    trace_record, spans = selected
                    selected_traces.append(trace_record)
                    for span in spans:
                        span_id = _stable_id(
                            DATASET_ID,
                            DEFAULT_RUN_ID,
                            trace_record["trace_id"],
                            span.get("spanID", ""),
                            trace_record["source_file"],
                        )
                        if span_id in seen_span_ids:
                            continue
                        seen_span_ids.add(span_id)
                        start_microseconds, _ = _span_interval_microseconds(span)
                        service_name = _process_service_name(
                            trace, span.get("processID")
                        )
                        parent_span_id = next(
                            (
                                reference.get("spanID")
                                for reference in span.get("references", [])
                                if reference.get("refType") == "CHILD_OF"
                            ),
                            None,
                        )
                        selected_spans.append(
                            {
                                "span_id": span_id,
                                "trace_id": trace_record["trace_id"],
                                "original_span_id": str(span.get("spanID", "")),
                                "parent_span_id": str(parent_span_id or ""),
                                "process_id": str(span.get("processID", "")),
                                "service_name": service_name or "",
                                "operation_name": str(
                                    span.get("operationName", "")
                                ),
                                "start_time_microseconds": start_microseconds,
                                "start_time": start_microseconds // 1_000_000,
                                "duration_microseconds": int(
                                    span.get("duration", 0)
                                ),
                                "source_file": trace_record["source_file"],
                                "overlaps_fault": any(
                                    _overlaps(span, fault.start_time, fault.end_time)
                                    for fault in faults
                                ),
                            }
                        )
                break
    except (OSError, tarfile.TarError, KeyError, ValueError, json.JSONDecodeError):
        raise

    if set(metadata) != {
        "start_end_times",
        "interfere",
        "train_interfere",
    }:
        missing = {
            "start_end_times",
            "interfere",
            "train_interfere",
        } - set(metadata)
        raise ValueError(f"Archive is missing metadata: {sorted(missing)}")

    collection_start, collection_end = _parse_collection_times(
        metadata["start_end_times"]
    )
    faults = _parse_fault_lines(metadata["interfere"], "interfere")
    faults += _parse_fault_lines(
        metadata["train_interfere"], "train_interfere"
    )
    return {
        "dataset_id": DATASET_ID,
        "run_id": DEFAULT_RUN_ID,
        "source_archive": archive_path.name,
        "fault_category": "compute",
        "collection_start": collection_start,
        "collection_end": collection_end,
        "faults": faults,
        "traces": selected_traces,
        "spans": selected_spans,
    }


def _research_queries() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            """
            MERGE (dataset:ResearchDataset {id: $dataset_id})
            SET dataset.name = $dataset_name,
                dataset.source_url = $source_url,
                dataset.doi = $doi,
                dataset.license = $license,
                dataset.benchmark = $benchmark,
                dataset.application = $application
            """,
            {
                "dataset_id": DATASET_ID,
                "dataset_name": "Murphy traces",
                "source_url": "https://databank.illinois.edu/datasets/IDB-6641912",
                "doi": "10.13012/B2IDB-6641912_V1",
                "license": "CC0",
                "benchmark": "DeathStarBench",
                "application": "hotel-reserv",
            },
        )
    ]


def write_records(graph: Any, parsed: dict[str, Any]) -> dict[str, int]:
    """Write only parsed records to the research graph using MERGE."""
    counts = {"datasets": 0, "runs": 0, "faults": 0, "components": 0, "traces": 0, "spans": 0}
    for query, params in _research_queries():
        graph.query(query, params=params)
    counts["datasets"] = 1
    graph.query(
        """
        MERGE (dataset:ResearchDataset {id: $dataset_id})
        MERGE (run:ResearchRun {id: $run_id})
        SET run.source_archive = $source_archive,
            run.fault_category = $fault_category,
            run.collection_start = $collection_start,
            run.collection_end = $collection_end
        MERGE (dataset)-[:HAS_RUN]->(run)
        """,
        params={k: parsed[k] for k in ("dataset_id", "run_id", "source_archive", "fault_category", "collection_start", "collection_end")},
    )
    counts["runs"] = 1

    for fault in parsed["faults"]:
        params = {
            "dataset_id": parsed["dataset_id"],
            "run_id": parsed["run_id"],
            **fault.__dict__,
        }
        graph.query(
            """
            MATCH (run:ResearchRun {id: $run_id})
            MERGE (fault:FaultInjection {id: $fault_id})
            SET fault.dataset_id = $dataset_id,
                fault.start_time = $start_time,
                fault.end_time = $end_time,
                fault.target_entity = $target_entity,
                fault.fail_param = $fail_param,
                fault.phase = $phase,
                fault.fault_category = $fault_category
            MERGE (run)-[:HAS_FAULT]->(fault)
            MERGE (component:ResearchComponent {id: $component_id})
            SET component.raw_identifier = $target_entity,
                component.identifier_kind = "container_or_pod",
                component.dataset_id = $dataset_id,
                component.run_id = $run_id
            MERGE (fault)-[:TARGETS]->(component)
            """,
            params={
                **params,
                "component_id": _stable_id(
                    parsed["dataset_id"],
                    parsed["run_id"],
                    "component",
                    fault.target_entity,
                ),
            },
        )
        counts["faults"] += 1
        counts["components"] += 1

    for trace in parsed["traces"]:
        graph.query(
            """
            MATCH (run:ResearchRun {id: $run_id})
            MERGE (trace:ResearchTrace {id: $trace_id})
            SET trace.dataset_id = $dataset_id,
                trace.source_file = $source_file,
                trace.span_count = $span_count
            MERGE (run)-[:HAS_TRACE]->(trace)
            """,
            params={"run_id": parsed["run_id"], "dataset_id": parsed["dataset_id"], **trace},
        )
        counts["traces"] += 1

    for span in parsed["spans"]:
        graph.query(
            """
            MATCH (trace:ResearchTrace {id: $trace_id})
            MERGE (span:ResearchSpan {id: $span_id})
            SET span.dataset_id = $dataset_id,
                span.trace_id = $trace_id,
                span.original_span_id = $original_span_id,
                span.parent_span_id = $parent_span_id,
                span.process_id = $process_id,
                span.service_name = $service_name,
                span.operation_name = $operation_name,
                span.start_time = $start_time,
                span.start_time_microseconds = $start_time_microseconds,
                span.duration_microseconds = $duration_microseconds,
                span.source_file = $source_file,
                span.overlaps_fault = $overlaps_fault
            MERGE (trace)-[:HAS_SPAN]->(span)
            """,
            params={"dataset_id": parsed["dataset_id"], **span},
        )
        if span["parent_span_id"]:
            graph.query(
                """
                MATCH (parent:ResearchSpan {
                    trace_id: $trace_id,
                    original_span_id: $parent_span_id
                })
                MATCH (child:ResearchSpan {id: $span_id})
                MERGE (parent)-[:PARENT_OF]->(child)
                """,
                params={
                    "parent_span_id": span["parent_span_id"],
                    "span_id": span["span_id"],
                    "trace_id": span["trace_id"],
                },
            )
        if span["service_name"]:
            graph.query(
                """
                MATCH (span:ResearchSpan {id: $span_id})
                MERGE (service:ResearchService {id: $service_id})
                SET service.name = $service_name,
                    service.mapping_basis = "Jaeger process serviceName"
                MERGE (span)-[:MAPPED_TO_SERVICE]->(service)
                """,
                params={
                    "span_id": span["span_id"],
                    "service_id": _stable_id(
                        parsed["dataset_id"], "service", span["service_name"]
                    ),
                    "service_name": span["service_name"],
                },
            )
        counts["spans"] += 1
    return counts


def _summary(parsed: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Dataset: {parsed['dataset_id']}",
            f"Run: {parsed['run_id']}",
            f"Collection: {parsed['collection_start']} - {parsed['collection_end']}",
            f"Faults parsed: {len(parsed['faults'])}",
            f"Relevant traces selected: {len(parsed['traces'])}",
            f"Relevant spans selected: {len(parsed['spans'])}",
            "Prometheus metrics: not parsed by this bounded pilot",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--graph-name", default=DEFAULT_GRAPH_NAME)
    parser.add_argument("--max-traces", type=int, default=DEFAULT_MAX_TRACES)
    parser.add_argument("--max-spans", type=int, default=DEFAULT_MAX_SPANS)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    if args.graph_name == PRODUCTION_GRAPH_NAME:
        parser.error(
            "Refusing to target incident_investigator; use "
            "incident_rehearsal_research."
        )
    if args.graph_name != DEFAULT_GRAPH_NAME:
        parser.error("The importer target must be incident_rehearsal_research.")

    try:
        parsed = inspect_archive(args.archive, args.max_traces, args.max_spans)
        print(_summary(parsed))
        if not args.write:
            print("Dry run: no FalkorDB writes performed.")
            return 0
        database = _create_database()
        try:
            graph = database.select_graph(DEFAULT_GRAPH_NAME)
            print(f"Imported records: {write_records(graph, parsed)}")
        finally:
            database.close()
        return 0
    except Exception as exc:
        print(
            "Murphy import failed: "
            f"{type(exc).__name__}; inspect the archive and configuration."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
