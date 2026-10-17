"""Demonstrate that FalkorDB relationships affect investigation-tool results."""

from __future__ import annotations

from typing import Any

from falkordb import FalkorDB

from tools import get_impacted_services


_GRAPH_NAME = "incident_investigator"
_FALKORDB_HOST = "localhost"
_FALKORDB_PORT = 6379

_CHECKOUT_ID = "checkout"
_AUTH_SERVICE_ID = "auth-service"

_CHECKOUT_AUTH_PATH = "Checkout -[:DEPENDS_ON]-> AuthService"
_CART_AUTH_PATH = "CartService -[:DEPENDS_ON]-> AuthService"
_MOBILE_CHECKOUT_AUTH_PATH = (
    "MobileAPI -[:DEPENDS_ON]-> Checkout -[:DEPENDS_ON]-> AuthService"
)


def _run_impacted_services() -> str:
    """Run the tested LangChain tool for AuthService."""
    return get_impacted_services.invoke({"service_name": "AuthService"})


def _delete_checkout_auth_relationship(graph: Any) -> None:
    """Temporarily delete only Checkout's dependency on AuthService."""
    graph.query(
        """
        MATCH (dependent:Service {id: $dependent_id})
              -[relationship:DEPENDS_ON]->
              (target:Service {id: $target_id})
        DELETE relationship
        """,
        params={
            "dependent_id": _CHECKOUT_ID,
            "target_id": _AUTH_SERVICE_ID,
        },
    )


def _restore_checkout_auth_relationship(graph: Any) -> None:
    """Restore Checkout's dependency on AuthService idempotently."""
    graph.query(
        """
        MATCH (dependent:Service {id: $dependent_id})
        MATCH (target:Service {id: $target_id})
        MERGE (dependent)-[:DEPENDS_ON]->(target)
        """,
        params={
            "dependent_id": _CHECKOUT_ID,
            "target_id": _AUTH_SERVICE_ID,
        },
    )


def _assert_baseline_result(result: str) -> None:
    """Ensure the graph has all expected paths before any mutation."""
    expected_paths = (
        _CHECKOUT_AUTH_PATH,
        _CART_AUTH_PATH,
        _MOBILE_CHECKOUT_AUTH_PATH,
    )
    missing_paths = [path for path in expected_paths if path not in result]

    if missing_paths:
        raise RuntimeError(
            "The baseline result is missing expected paths: "
            + "; ".join(missing_paths)
            + ". The graph was not modified."
        )


def _assert_during_result(result: str) -> None:
    """Ensure only CartService remains affected during the deletion."""
    if _CART_AUTH_PATH not in result:
        raise RuntimeError(
            "The temporary deletion result did not contain the expected "
            "CartService -> AuthService path."
        )

    removed_paths = (_CHECKOUT_AUTH_PATH, _MOBILE_CHECKOUT_AUTH_PATH)
    remaining_removed_paths = [
        path for path in removed_paths if path in result
    ]

    if remaining_removed_paths:
        raise RuntimeError(
            "The temporary deletion result still contained expected-removed "
            "paths: "
            + "; ".join(remaining_removed_paths)
        )


def _assert_restored_result(result: str) -> None:
    """Ensure restoration makes all expected paths visible again."""
    expected_paths = (
        _CHECKOUT_AUTH_PATH,
        _CART_AUTH_PATH,
        _MOBILE_CHECKOUT_AUTH_PATH,
    )
    missing_paths = [path for path in expected_paths if path not in result]

    if missing_paths:
        raise RuntimeError(
            "The restored relationship result was missing expected paths: "
            + "; ".join(missing_paths)
        )


def main() -> None:
    """Run the before, during, and after-restoration verification."""
    db: FalkorDB | None = None
    graph: Any | None = None
    deletion_attempted = False
    verification_errors: list[BaseException] = []

    try:
        db = FalkorDB(host=_FALKORDB_HOST, port=_FALKORDB_PORT)
        graph = db.select_graph(_GRAPH_NAME)

        print("=== BEFORE RELATIONSHIP DELETION ===")
        before_result = _run_impacted_services()
        print(before_result)
        _assert_baseline_result(before_result)

        print("\n=== DURING TEMPORARY RELATIONSHIP DELETION ===")
        deletion_attempted = True
        _delete_checkout_auth_relationship(graph)

        during_result = _run_impacted_services()
        print(during_result)
        _assert_during_result(during_result)

    except Exception as exc:
        verification_errors.append(exc)
        print(f"\nVerification error: {type(exc).__name__}: {exc}")

    finally:
        if graph is not None and deletion_attempted:
            try:
                _restore_checkout_auth_relationship(graph)
            except Exception as exc:
                verification_errors.append(exc)
                print(
                    "Relationship restoration failed: "
                    f"{type(exc).__name__}: {exc}"
                )

            print("\n=== AFTER RESTORATION ===")
            try:
                after_result = _run_impacted_services()
                print(after_result)
                _assert_restored_result(after_result)
            except Exception as exc:
                verification_errors.append(exc)
                print(
                    "Restoration verification error: "
                    f"{type(exc).__name__}: {exc}"
                )

        if db is not None:
            try:
                db.close()
            except Exception as exc:
                verification_errors.append(exc)
                print(
                    "Database cleanup failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    if verification_errors:
        raise SystemExit(1)

    print("\nGraph relationship effect verified successfully.")


if __name__ == "__main__":
    main()
