"""Focused tests for the bounded Murphy parser."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from import_murphy_traces import (
    _iter_json_array_items,
    _overlaps,
    _parse_fault_lines,
    inspect_archive,
)
from tools import analyze_murphy_fault
from app import _build_murphy_topology_graph, _derive_murphy_topology


class MurphyImporterTests(unittest.TestCase):
    def test_metadata_and_fault_parsing(self) -> None:
        faults = _parse_fault_lines(
            "10 20 rate-pod 2\n30 40 search-pod 3\n",
            "interfere",
        )
        self.assertEqual(len(faults), 2)
        self.assertEqual(faults[0].target_entity, "rate-pod")
        self.assertEqual(faults[1].start_time, 30)

    def test_streaming_json_array_parser(self) -> None:
        records = list(
            _iter_json_array_items(
                io.BytesIO(b'[{"id": 1}, {"id": 2}]'),
                chunk_size=5,
            )
        )
        self.assertEqual(records, [{"id": 1}, {"id": 2}])

    def test_timestamp_overlap_cases(self) -> None:
        self.assertFalse(
            _overlaps({"startTime": 1_000_000, "duration": 2_000_000}, 5, 10)
        )
        self.assertFalse(
            _overlaps({"startTime": 11_000_000, "duration": 2_000_000}, 5, 10)
        )
        self.assertTrue(
            _overlaps({"startTime": 9_500_000, "duration": 2_000_000}, 5, 10)
        )
        self.assertTrue(
            _overlaps({"startTime": 6_000_000, "duration": 1_000_000}, 5, 10)
        )

    def test_timestamp_overlap_rejects_observed_after_end_false_positive(self) -> None:
        span = {"startTime": 30_000_001_000, "duration": 500_000}
        self.assertFalse(_overlaps(span, 20, 30))

    def test_analysis_marks_missing_timestamps_unknown(self) -> None:
        result = {
            "fault": {"start_time": 20, "end_time": 30},
            "spans": [
                {
                    "trace_id": "trace-1",
                    "original_span_id": "span-1",
                    "duration_microseconds": 1_000_000,
                }
            ],
        }
        analysis = analyze_murphy_fault(result)
        self.assertEqual(len(analysis["unknown_timestamp_spans"]), 1)
        self.assertEqual(
            result["spans"][0]["overlaps_selected_fault"], "unknown"
        )

    def test_exact_fault_analysis_groups_and_keeps_sample_boundary(self) -> None:
        result = {
            "fault": {"start_time": 20, "end_time": 30},
            "spans": [
                {
                    "trace_id": "trace-1",
                    "original_span_id": "span-1",
                    "start_time_microseconds": 25_000_000,
                    "duration_microseconds": 1_000_000,
                    "mapped_service": "rate",
                    "operation_name": "GET /",
                    "parent_span_id": "parent-1",
                    "resolved_parent_span_id": "parent-1",
                    "source_file": "traces1.json",
                },
                {
                    "trace_id": "trace-2",
                    "original_span_id": "span-2",
                    "start_time_microseconds": 40_000_000,
                    "duration_microseconds": 1_000_000,
                    "mapped_service": "search",
                    "operation_name": "GET /search",
                },
            ],
        }
        analysis = analyze_murphy_fault(result)
        self.assertEqual(analysis["overlapping_traces"], ["trace-1"])
        self.assertEqual(analysis["overlapping_spans"], ["span-1"])
        self.assertEqual(analysis["breakdown"][0]["Overlapping spans"], 1)
        self.assertEqual(len(analysis["parent_child_paths"]), 1)
        self.assertEqual(len(analysis["sample_only_spans"]), 1)

    def test_murphy_topology_uses_explicit_mapped_parent_edges(self) -> None:
        topology = _derive_murphy_topology(
            [
                {
                    "mapped_service": "frontend",
                    "parent_mapped_service": "search",
                    "resolved_parent_span_id": "parent-1",
                    "original_span_id": "span-1",
                    "overlaps_selected_fault": True,
                },
                {
                    "mapped_service": "frontend",
                    "parent_mapped_service": "frontend",
                    "resolved_parent_span_id": "parent-2",
                    "original_span_id": "span-2",
                    "overlaps_selected_fault": False,
                },
            ],
            "frontend-pod",
        )
        self.assertEqual(topology["edges"], [("search", "frontend")])
        self.assertEqual(topology["overlapping_services"], ["frontend"])
        self.assertIn("search", _build_murphy_topology_graph(topology))
        self.assertIn("frontend", _build_murphy_topology_graph(topology))

    def test_murphy_topology_keeps_unknown_target_unresolved(self) -> None:
        topology = _derive_murphy_topology(
            [
                {
                    "mapped_service": "",
                    "parent_mapped_service": "search",
                    "resolved_parent_span_id": "parent-1",
                    "original_span_id": "span-1",
                }
            ],
            "search-abc123",
        )
        self.assertFalse(topology["target_is_resolved"])
        self.assertEqual(topology["target_label"], "Target component\nsearch-abc123")
        self.assertEqual(topology["edges"], [])
        graph = _build_murphy_topology_graph(topology)
        self.assertIn("Target component", graph)
        self.assertIn("search-abc123", graph)

    def test_bounded_archive_selection_and_duplicate_ids(self) -> None:
        trace = {
            "traceID": "trace-1",
            "processes": {"p1": {"serviceName": "rate"}},
            "spans": [
                {
                    "spanID": "span-1",
                    "processID": "p1",
                    "operationName": "GET /",
                    "startTime": 25_000_000,
                    "duration": 20_000,
                    "references": [],
                    "tags": [{"key": "span.kind", "value": "server"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "compute-12.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                files = {
                    "compute-12/start_end_times": "10\n40\n",
                    "compute-12/interfere": "20 30 rate-pod 2\n",
                    "compute-12/train_interfere": "",
                    "compute-12/jaeger_traces/traces1.json": json.dumps(
                        {"data": [trace, trace]}
                    ),
                }
                for name, content in files.items():
                    data = content.encode()
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

            parsed = inspect_archive(archive_path, max_traces=1, max_spans=1)

        self.assertEqual(len(parsed["faults"]), 1)
        self.assertEqual(len(parsed["traces"]), 1)
        self.assertEqual(len(parsed["spans"]), 1)
        self.assertTrue(parsed["spans"][0]["overlaps_fault"])

    def test_production_graph_is_not_a_valid_target(self) -> None:
        with self.assertRaises(SystemExit):
            from import_murphy_traces import main

            main(
                [
                    "--archive",
                    "missing.tar.gz",
                    "--graph-name",
                    "incident_investigator",
                ]
            )


if __name__ == "__main__":
    unittest.main()
