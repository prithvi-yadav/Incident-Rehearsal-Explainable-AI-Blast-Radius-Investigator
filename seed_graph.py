"""Seed the incident investigator graph with fictional service data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from dotenv import load_dotenv
from falkordb import FalkorDB


load_dotenv()

_DEFAULT_GRAPH_NAME = "incident_investigator"
_DEFAULT_FALKORDB_HOST = "localhost"
_DEFAULT_FALKORDB_PORT = 6379


SERVICES = [
    {"id": "mobile-api", "name": "MobileAPI", "status": "healthy"},
    {"id": "checkout", "name": "Checkout", "status": "degraded"},
    {"id": "cart-service", "name": "CartService", "status": "degraded"},
    {"id": "auth-service", "name": "AuthService", "status": "failing"},
    {"id": "user-db", "name": "UserDB", "status": "healthy"},
]

DEPENDENCIES = [
    ("mobile-api", "checkout"),
    ("checkout", "auth-service"),
    ("cart-service", "auth-service"),
    ("auth-service", "user-db"),
]


def _setting(name: str, default: str | None = None) -> str | None:
    """Read a non-secret local environment setting."""
    value = os.getenv(name)
    return value.strip() if value else default


def _setting_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment setting."""
    value = _setting(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "on"}


def _connection_kwargs() -> dict[str, Any]:
    """Build FalkorDB options using the same conventions as tools.py."""
    port_value = _setting("FALKORDB_PORT", str(_DEFAULT_FALKORDB_PORT))
    try:
        port = int(port_value or _DEFAULT_FALKORDB_PORT)
    except ValueError as exc:
        raise ValueError("FALKORDB_PORT must be a valid integer.") from exc

    kwargs: dict[str, Any] = {
        "host": _setting("FALKORDB_HOST", _DEFAULT_FALKORDB_HOST),
        "port": port,
        "ssl": _setting_bool("FALKORDB_SSL"),
    }
    for option, setting_name in {
        "username": "FALKORDB_USERNAME",
        "password": "FALKORDB_PASSWORD",
    }.items():
        value = _setting(setting_name)
        if value is not None:
            kwargs[option] = value
    return kwargs


def _create_database() -> FalkorDB:
    """Create a FalkorDB client from a URL or explicit connection settings."""
    url = _setting("FALKORDB_URL")
    if url:
        url_kwargs = _connection_kwargs()
        url_kwargs.pop("host", None)
        url_kwargs.pop("port", None)
        url_kwargs.pop("ssl", None)
        return FalkorDB.from_url(url, **url_kwargs)

    return FalkorDB(**_connection_kwargs())


def _safe_connection_error(exc: Exception) -> str:
    """Return connection guidance without exposing credentials or URLs."""
    return (
        f"{type(exc).__name__}; check FalkorDB availability, host, port, "
        "credentials, TLS settings, and graph name."
    )


def _print_result_rows(result: Any) -> None:
    """Print rows from a FalkorDB result using the official result_set API."""
    for row in result.result_set:
        print("  ", *row)


def main() -> None:
    """Create the service graph and print the seeded data and paths."""
    db: FalkorDB | None = None

    try:
        db = _create_database()
        graph_name = _setting("FALKORDB_GRAPH_NAME", _DEFAULT_GRAPH_NAME)
        graph = db.select_graph(graph_name)

        service_query = """
        MERGE (service:Service {id: $id})
        SET service.name = $name,
            service.status = $status
        RETURN service.id, service.name, service.status
        """

        for service in SERVICES:
            graph.query(service_query, params=service)

        dependency_query = """
        MATCH (dependent:Service {id: $dependent_id})
        MATCH (target:Service {id: $target_id})
        MERGE (dependent)-[:DEPENDS_ON]->(target)
        RETURN dependent.name, target.name
        """

        for dependent_id, target_id in DEPENDENCIES:
            graph.query(
                dependency_query,
                params={
                    "dependent_id": dependent_id,
                    "target_id": target_id,
                },
            )

        auth_service_evidence = {
            "developer": {
                "id": "alice",
                "name": "Alice",
                "email": "alice@example.com",
                "on_call": True,
            },
            "deployment": {
                "id": "deploy-v2.4",
                "version": "2.4",
                "commit_hash": "abc1234",
                "timestamp": (
                    datetime.now(timezone.utc) - timedelta(minutes=25)
                ).isoformat(),
                "description": "Authentication service deployment",
            },
            "incident": {
                "id": "INC-88",
                "title": "Authentication memory leak",
                "root_cause": "Memory usage increased after a previous release",
                "resolution": "Rolled back the affected release",
            },
            "runbook": {
                "id": "RB-AUTH-01",
                "title": "Authentication Service Troubleshooting",
                "url": "https://example.com/runbooks/auth-service",
            },
        }

        developer_query = """
        MERGE (developer:Developer {id: $id})
        SET developer.name = $name,
            developer.email = $email,
            developer.on_call = $on_call
        WITH developer
        MATCH (service:Service {id: $service_id})
        MERGE (service)-[:MAINTAINED_BY]->(developer)
        RETURN developer.id, service.name
        """

        graph.query(
            developer_query,
            params={
                **auth_service_evidence["developer"],
                "service_id": "auth-service",
            },
        )

        deployment_query = """
        MERGE (deployment:Deployment {id: $id})
        SET deployment.version = $version,
            deployment.commit_hash = $commit_hash,
            deployment.timestamp = $timestamp,
            deployment.description = $description
        WITH deployment
        MATCH (service:Service {id: $service_id})
        MERGE (deployment)-[:DEPLOYED_TO]->(service)
        RETURN deployment.id, service.name
        """

        graph.query(
            deployment_query,
            params={
                **auth_service_evidence["deployment"],
                "service_id": "auth-service",
            },
        )

        incident_query = """
        MERGE (incident:PastIncident {id: $id})
        SET incident.title = $title,
            incident.root_cause = $root_cause,
            incident.resolution = $resolution
        WITH incident
        MATCH (service:Service {id: $service_id})
        MERGE (incident)-[:AFFECTED]->(service)
        RETURN incident.id, service.name
        """

        graph.query(
            incident_query,
            params={
                **auth_service_evidence["incident"],
                "service_id": "auth-service",
            },
        )

        runbook_query = """
        MERGE (runbook:Runbook {id: $id})
        SET runbook.title = $title,
            runbook.url = $url
        WITH runbook
        MATCH (service:Service {id: $service_id})
        MERGE (service)-[:DOCUMENTED_IN]->(runbook)
        RETURN runbook.id, service.name
        """

        graph.query(
            runbook_query,
            params={
                **auth_service_evidence["runbook"],
                "service_id": "auth-service",
            },
        )

        print("Seeded services:")
        service_result = graph.query(
            """
            MATCH (service:Service)
            RETURN service.id, service.name, service.status
            ORDER BY service.id
            """
        )
        _print_result_rows(service_result)

        print("\nDependency paths leading to AuthService:")
        path_result = graph.query(
            """
            MATCH path = (dependent:Service)-[:DEPENDS_ON*1..5]->
                         (target:Service)
            WHERE target.id = $target_id
            RETURN [node IN nodes(path) | node.name] AS service_path,
                   length(path) AS hops
            ORDER BY hops, service_path
            """,
            params={"target_id": "auth-service"},
        )

        for row in path_result.result_set:
            service_path, hops = row
            print(f"  {' -> '.join(service_path)} ({hops} hop(s))")

        print("\nAdditional investigation evidence:")
        print("  Developer: Alice maintains AuthService")
        print("  Deployment: deploy-v2.4 deployed to AuthService")
        print("  Past incident: INC-88 affected AuthService")
        print("  Runbook: RB-AUTH-01 documents AuthService")

        print("\nGraph seeding completed successfully.")

    except Exception as exc:
        print(
            "Unable to seed the incident investigator graph. "
            f"{_safe_connection_error(exc)}"
        )
        raise
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
