"""LangChain agent for investigating incidents with FalkorDB graph evidence."""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_google_genai import ChatGoogleGenerativeAI

from tools import (
    get_historical_incidents,
    get_impacted_services,
    get_recent_deployments,
    get_service_blast_radius,
    get_service_dependencies,
    get_service_owner_and_runbook,
    incident_tools,
)


_MODEL_NAME = "gemini-3.8-flash"
logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are an AI incident investigator using a FalkorDB graph through tools.

Investigate the reported incident rather than merely summarizing it. Identify
the failing service from the incident description, then call all registered
tools for that service, including the blast-radius measurement:
1. impacted services,
2. recent deployments,
3. historical incidents,
4. service owner and runbook,
5. service blast radius.

Use the actual tool results as evidence. Never invent graph relationships,
source records, owners, timestamps, causes, resolutions, or operational
properties that are not explicitly present in the incident description or tool
results.

Evidence rules:
- Keep observations from the incident description separate from facts recorded
  in FalkorDB.
- A dependency path shows a potential impact relationship only. It does not
  prove that every connected service is currently failing, that the dependency
  is synchronous, that it is critical, or that it caused the incident unless
  the retrieved records explicitly establish that.
- Do not infer cascading failures from a historical incident unless the stored
  evidence explicitly states that cascading failures occurred.
- A recent deployment is a clue for investigation, not proof that it caused
  the current incident.
- A similar historical incident is a clue for investigation, not proof of the
  current root cause.
- The absence of a graph record is unknown information. Do not treat missing
  deployments, incidents, owners, runbooks, or relationships as evidence that
  a cause is unlikely.
- Do not assign Low, Medium, or High confidence scores, percentages, or other
  confidence ratings to hypotheses unless a defined and calibrated scoring
  method is provided in the evidence. Without such a method, describe the
  evidential support in plain language only.
- If a tool returns no records or an error, report that result explicitly
  without drawing negative conclusions from the absence of records.

Clearly separate the following categories in the report:
- Facts recorded in FalkorDB: claims directly supported by tool output.
- Observations from the incident description: claims supplied by the reporter,
  not independently verified by the graph.
- Possible explanations requiring further investigation: hypotheses tied to
  specific evidence, clearly labeled as unproven.
- Unknown information: details not established by the incident description or
  retrieved records.

Explain direct and indirect dependency paths using the actual graph direction.
When assessing potential blast radius, use the measured unique dependent counts,
shortest-path classifications, and maximum distance returned by the blast-radius
tool. Do not turn these measurements into arbitrary criticality labels or
confidence scores.
Describe potential impact without claiming current service failure, criticality,
synchronous behavior, or causality unless the records explicitly establish it.
Retrieve and report relevant historical incidents, owners, and runbooks.
Recommend practical next investigation steps that could distinguish among the
possible explanations.

Produce a structured Markdown incident report with these sections:
1. Incident summary
2. Recorded facts
3. Impacted services and dependency paths
4. Recent deployment evidence
5. Historical incident evidence
6. Service owner and runbook
7. Possible causes and supporting evidence
8. Unknowns and limitations
9. Recommended next investigation steps

In section 7, preserve the required section heading but do not provide
uncalibrated confidence scores. Instead, state the evidential basis and label
each item as a possible explanation requiring further investigation.

Do not claim to have found the root cause unless the retrieved evidence
explicitly establishes it.
""".strip()


class ToolTraceHandler(BaseCallbackHandler):
    """Print a minimal trace of tools invoked by the agent."""

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Print the tool name when a tool invocation starts."""
        tool_name = (serialized or {}).get("name", "unknown_tool")
        print(f"[tool start] {tool_name}")

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        """Confirm that the current tool invocation completed."""
        print("[tool end] completed")

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        """Report a tool failure without exposing sensitive configuration."""
        print(f"[tool error] {type(error).__name__}: {error}")


def _create_agent() -> Any:
    """Create the incident investigation agent after loading API configuration."""
    load_dotenv()

    api_key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or _streamlit_secret("GEMINI_API_KEY")
        or _streamlit_secret("GOOGLE_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "Gemini API key is not configured. Set GEMINI_API_KEY or "
            "GOOGLE_API_KEY in the environment, .env file, or Streamlit "
            "Community Cloud secrets."
        )

    model = ChatGoogleGenerativeAI(
        model=_MODEL_NAME,
        google_api_key=api_key,
    )

    return create_agent(
        model=model,
        tools=incident_tools,
        system_prompt=_SYSTEM_PROMPT,
    )


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


def _final_response(agent_result: dict[str, Any]) -> str:
    """Extract the final assistant response from the agent state."""
    messages = agent_result.get("messages", [])
    if not messages:
        raise RuntimeError("The incident agent returned no messages.")

    content = messages[-1].content
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("text")
        ]
        if text_blocks:
            return "\n".join(text_blocks)

    return str(content)


def _is_quota_exhaustion(error: BaseException) -> bool:
    """Return whether an exception clearly indicates Gemini quota exhaustion."""
    seen: set[int] = set()
    current: BaseException | None = error

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status_code = getattr(current, "status_code", None)
        if status_code == 429:
            return True

        message = str(current).upper()
        if "RESOURCE_EXHAUSTED" in message:
            return True
        if "429" in message and (
            "QUOTA" in message
            or "RATE LIMIT" in message
            or "TOO MANY REQUESTS" in message
        ):
            return True

        current = current.__cause__ or current.__context__

    return False


def _run_fallback_tool(tool: Any, service_name: str) -> str:
    """Invoke one deterministic LangChain tool without an LLM."""
    return str(tool.invoke({"service_name": service_name}))


def generate_deterministic_investigation(
    incident_description: str,
    service_name: str,
) -> str:
    """
    Generate an evidence-only report from FalkorDB tools without an LLM.

    This path is intended for Gemini quota exhaustion. Tool responses are
    included as retrieved evidence and are not interpreted as proof of outages
    or root cause.
    """
    description = incident_description.strip()
    selected_service = service_name.strip()
    if not description:
        raise ValueError("incident_description must not be empty.")
    if not selected_service:
        raise ValueError("service_name must not be empty.")

    blast_radius = _run_fallback_tool(
        get_service_blast_radius,
        selected_service,
    )
    dependencies = _run_fallback_tool(
        get_service_dependencies,
        selected_service,
    )
    impacted = _run_fallback_tool(get_impacted_services, selected_service)
    deployments = _run_fallback_tool(
        get_recent_deployments,
        selected_service,
    )
    incidents = _run_fallback_tool(
        get_historical_incidents,
        selected_service,
    )
    ownership = _run_fallback_tool(
        get_service_owner_and_runbook,
        selected_service,
    )

    return f"""# Deterministic incident investigation

> Gemini was unavailable because its API quota was exhausted. This report was
> generated deterministically from FalkorDB tool results without calling an
> LLM.

## Incident summary

**Selected service:** `{selected_service}`

### Reporter-provided symptoms

{description}

## Recorded facts

The following content is returned directly by the FalkorDB investigation tools:

### Blast-radius measurement

{blast_radius}

### Direct dependencies

{dependencies}

### Recent deployments

{deployments}

### Historical incidents

{incidents}

### Service owner and runbook

{ownership}

## Potential impact

The impacted-service results below describe dependency paths that may indicate
structural exposure within the queried traversal limit. They do not prove that
any connected service is currently failing.

{impacted}

## Possible explanations requiring further investigation

- A recent deployment may be relevant only if its recorded timing and details
  match the incident timeline. The deployment records above do not, by
  themselves, establish causality.
- A historical incident may provide a comparison point, but it does not prove
  that the same cause or failure pattern is present now.
- The measured dependency paths may help prioritize investigation, but they do
  not establish synchronous behavior, business criticality, or a root cause.

## Unknowns and limitations

- This deterministic report does not validate live service health or customer
  impact beyond the reporter's description.
- Missing graph records are unknown information, not evidence that an event or
  relationship did not occur.
- No confidence or criticality rating has been assigned because no calibrated
  scoring method was provided.
"""


def investigate_incident(
    incident_description: str,
    service_name: str | None = None,
) -> str:
    """
    Investigate an incident and return a structured Markdown report.

    The agent is given the registered FalkorDB tools and is instructed to gather
    graph evidence before forming conclusions.
    """
    description = incident_description.strip()
    if not description:
        raise ValueError("incident_description must not be empty.")

    selected_service = (service_name or "").strip()
    user_content = description
    if selected_service:
        user_content = (
            f"Focus the investigation on the selected service "
            f"'{selected_service}'.\n\n{description}"
        )

    try:
        agent = _create_agent()
        trace_handler = ToolTraceHandler()

        result = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": user_content,
                    }
                ]
            },
            config={
                "callbacks": [trace_handler],
                # Prevent an accidental unbounded tool/model loop.
                "recursion_limit": 12,
            },
        )

        return _final_response(result)
    except Exception as exc:
        if not _is_quota_exhaustion(exc):
            raise
        if not selected_service:
            raise ValueError(
                "A selected service is required for deterministic fallback."
            ) from exc

        logger.warning(
            "Gemini quota exhausted; using deterministic FalkorDB fallback "
            "for service %s",
            selected_service,
        )
        return generate_deterministic_investigation(
            description,
            selected_service,
        )


def main() -> None:
    """Run the first fictional AuthService investigation."""
    incident_description = (
        "AuthService is failing and customers are reporting Checkout timeouts. "
        "Investigate the potential impact, recent deployments, historical "
        "incidents, service owner, and runbook. Do not assume the root cause."
    )

    try:
        report = investigate_incident(incident_description)
        print("\n--- Incident Investigation Report ---")
        print(report)
    except Exception as exc:
        print(
            "Incident investigation failed. "
            f"{type(exc).__name__}: {exc}"
        )


if __name__ == "__main__":
    main()
