# Incident Rehearsal

> **AI-powered blast-radius investigation for software incidents, backed by a FalkorDB graph containing synthetic demonstration data.**

Incident Rehearsal helps engineers investigate a reported software incident by tracing service dependencies, measuring potential blast radius, retrieving operational evidence, and rehearsing hypothetical dependency isolation without changing the stored graph.

Built for **Graph Hacks: Context for AI Agents** by WeMakeDevs and FalkorDB.

This repository and its documentation were prepared with assistance from an AI
coding assistant. All implementation claims and setup notes should be reviewed
against the source code and local environment before production use.

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.66.0-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![FalkorDB](https://img.shields.io/badge/FalkorDB-graph_database-7B61FF)](https://www.falkordb.com/)
[![LangChain](https://img.shields.io/badge/LangChain-agent-1C3C3C)](https://www.langchain.com/)
[![Gemini](https://img.shields.io/badge/Google%20Gemini-LLM-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)

## Table of contents

- [Problem statement](#problem-statement)
- [Key features](#key-features)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Graph data model](#graph-data-model)
- [Cypher queries and algorithms](#cypher-queries-and-algorithms)
- [Installation and local setup](#installation-and-local-setup)
- [Using the application](#using-the-application)
- [Verification and testing](#verification-and-testing)
- [Evidence and safety boundaries](#evidence-and-safety-boundaries)
- [Project structure](#project-structure)
- [Limitations and future work](#limitations-and-future-work)
- [License](#license)

## Problem statement

During an outage, responders need more than a list of services. They need to understand:

- Which services depend directly or indirectly on the affected service.
- How many unique services may be exposed through those dependency paths.
- Whether a recent deployment is relevant to the incident timeline.
- Whether a previous incident contains documented causes or resolutions.
- Who owns the service and where its operational runbook is located.

Incident Rehearsal combines these records in FalkorDB and presents them through a Streamlit interface. A LangChain agent can gather the evidence and write a structured report. When Gemini quota exhaustion is detected, the application can produce a deterministic report directly from the same graph tools without making another LLM call.

The application deliberately does **not** treat a dependency path as proof that a service is failing, that a dependency is synchronous or business-critical, or that a particular event caused the incident. Graph relationships describe structural exposure and investigation clues; responders must validate real-world behavior separately.

## Key features

### FalkorDB-powered service graph

- Uses the `incident_investigator` FalkorDB graph.
- Reads service nodes, recorded statuses, and `DEPENDS_ON` relationships for the live dependency visualization.
- Uses read-only `ro_query()` calls for investigation tools.

### LangChain investigation agent

The Gemini-powered agent is created with the predefined tools in `incident_tools`:

1. `get_impacted_services`
2. `get_service_dependencies`
3. `get_service_blast_radius`
4. `get_recent_deployments`
5. `get_historical_incidents`
6. `get_service_owner_and_runbook`

The agent is instructed to separate reporter observations, FalkorDB facts, unproven hypotheses, and unknown information.

### Dependency and blast-radius analysis

- Traverses impacted-service paths up to five hops.
- Measures blast radius up to three hops.
- Counts unique dependent services rather than paths.
- Assigns each dependent its shortest path distance.
- Separates direct dependents from indirect dependents.
- Reports the maximum shortest-path distance and actual graph paths.

### Operational evidence retrieval

The tools retrieve, where present:

- Recent deployments within the previous 24 hours, with safe timestamp parsing.
- Historical incidents, documented root causes, and resolutions.
- Service maintainers, email addresses, on-call status, and runbooks.

### Deterministic Gemini-quota fallback

If the Gemini path clearly reports HTTP `429` or `RESOURCE_EXHAUSTED` quota exhaustion, `agent.py` invokes the existing graph tools directly in Python and creates an evidence-only Markdown report. Unrelated programming errors and FalkorDB failures are not silently classified as quota exhaustion.

### Incident Rehearsal

The Incident Rehearsal tab:

- Selects an actual incoming dependency edge for the focused service.
- Removes exactly that directed edge from an in-memory copy of the edge list.
- Recomputes before-and-after reachability and blast-radius measurements.
- Shows Before and After Graphviz panels.
- Highlights the selected service, remaining reachable dependents, and dependents that are no longer reachable.
- Does not write to FalkorDB and does not call Gemini.

### Automatic Scenario Analyzer

The `Compare All Scenarios` action evaluates every actual incoming edge to the selected service, ranks scenarios by the number of dependent services that become unreachable, and uses a deterministic alphabetical tie-breaker. Results can be selected to view the same Before/After graphs and metrics.

The ranking is a graph-reachability comparison, not a recommended production fix or a safety guarantee.

### Streamlit dashboard and report download

The Streamlit page provides:

- Overview, Incident Rehearsal, and Investigation Report tabs.
- A focus-service selector populated from FalkorDB.
- A hierarchical left-to-right 2D Graphviz graph.
- Status-colored service nodes and focused-service highlighting.
- A complete Markdown report and a Markdown download button.

## Architecture

```mermaid
flowchart LR
    UI[Streamlit UI]
    Agent[LangChain agent]
    Tools[Predefined Python tools]
    Graph[(FalkorDB<br/>incident_investigator)]
    Fallback[Deterministic Python report<br/>on Gemini quota exhaustion]
    Sim[In-memory rehearsal simulation]

    UI -->|incident + focus service| Agent
    Agent -->|tool calls| Tools
    Tools -->|read-only Cypher| Graph
    Tools -->|evidence| Agent
    Agent -->|structured report| UI
    Agent -.->|429 / RESOURCE_EXHAUSTED| Fallback
    Fallback -->|reuse tools| Tools
    UI -->|selected edge| Sim
    Sim -->|copy edges, remove one edge| UI
```

The graph visualization and rehearsal calculations use graph data loaded by the UI. Rehearsal removes an edge only from an in-memory Python list; it never issues a database mutation.

## Technology stack

| Technology | Role |
|---|---|
| Python | Application, graph algorithms, scripts, and deterministic report generation |
| Streamlit | Dashboard layout, tabs, controls, Graphviz rendering, report display, and download |
| FalkorDB | Property-graph storage and read-only Cypher queries |
| Cypher | Service dependency and operational-evidence queries |
| LangChain | Tool definitions and agent orchestration |
| Google Gemini | Preferred LLM investigation path through `langchain-google-genai` |
| Graphviz | 2D dependency graph rendering through Streamlit's `st.graphviz_chart()` |
| `python-dotenv` | Loads local Gemini and FalkorDB configuration from `.env` |

`requirements.txt` uses exact `==` version pins. The current local virtual environment was inspected on October 17, 2026 and reported:

| Package | Installed version |
|---|---:|
| `falkordb` | 1.7.1 |
| `streamlit` | 1.66.0 |
| `langchain` | 1.4.4 |
| `langchain-core` | 1.6.9 |
| `langchain-google-genai` | 4.4.1 |
| `google-genai` | 2.29.0 |
| `python-dotenv` | 1.2.4 |
| `pydantic` | 2.14.0 |
| `rich` | 15.0.0 |

## Graph data model

The graph is seeded by `seed_graph.py` with synthetic demonstration data. It is not production telemetry.

### Node labels and properties

| Label | Properties used |
|---|---|
| `Service` | `id`, `name`, `status` |
| `Developer` | `id`, `name`, `email`, `on_call` |
| `Deployment` | `id`, `version`, `commit_hash`, `timestamp`, `description` |
| `PastIncident` | `id`, `title`, `root_cause`, `resolution` |
| `Runbook` | `id`, `title`, `url` |

### Relationship types

| Relationship | Direction and meaning |
|---|---|
| `DEPENDS_ON` | `Service` → `Service`; A depends on B |
| `MAINTAINED_BY` | `Service` → `Developer` |
| `DEPLOYED_TO` | `Deployment` → `Service` |
| `AFFECTED` | `PastIncident` → `Service` |
| `DOCUMENTED_IN` | `Service` → `Runbook` |

The dependency direction is important:

```text
A → B means service A depends on service B.
```

For a selected target `B`, impacted services are found by following incoming paths from dependent services toward `B`.

```mermaid
graph LR
    MobileAPI[Service<br/>MobileAPI] -->|DEPENDS_ON| Checkout[Service<br/>Checkout]
    Checkout -->|DEPENDS_ON| Auth[Service<br/>AuthService]
    Cart[Service<br/>CartService] -->|DEPENDS_ON| Auth
    Auth -->|DEPENDS_ON| UserDB[Service<br/>UserDB]

    Deploy[Deployment] -->|DEPLOYED_TO| Auth
    Past[PastIncident] -->|AFFECTED| Auth
    Auth -->|MAINTAINED_BY| Alice[Developer]
    Auth -->|DOCUMENTED_IN| Runbook[Runbook]
```

The current seed data creates five services and four dependency edges:

- `MobileAPI → Checkout`
- `Checkout → AuthService`
- `CartService → AuthService`
- `AuthService → UserDB`

It also creates synthetic AuthService evidence for Alice, deployment `deploy-v2.4`, incident `INC-88`, and runbook `RB-AUTH-01`.

## Cypher queries and algorithms

The following examples are representative of the exact parameterized queries implemented in `tools.py`.

### Direct dependencies of a service

```cypher
MATCH p = (service:Service)-[:DEPENDS_ON]->(dependency:Service)
WHERE service.name = $service_name
RETURN p,
       service.name,
       dependency.name,
       dependency.status
```

This follows outgoing edges from the selected service to identify what it depends on.

### Direct and indirect dependents

```cypher
MATCH p = (dependent:Service)-[:DEPENDS_ON*1..5]->(target:Service)
WHERE target.name = $service_name
RETURN p, length(p) AS hops
ORDER BY hops, dependent.name
```

This returns graph paths leading to the selected target. The blast-radius tool uses a similar read-only query bounded to `1..3` hops and includes dependent identifiers and names.

### Recent deployments

```cypher
MATCH p = (deployment:Deployment)-[:DEPLOYED_TO]->(service:Service)
WHERE service.name = $service_name
RETURN p,
       deployment.version,
       deployment.commit_hash,
       deployment.timestamp,
       deployment.description
```

Python parses ISO 8601 values and Unix timestamps in seconds. Only timestamps between the current UTC time and 24 hours earlier are reported as recent. Missing, invalid, and out-of-range timestamps are skipped and reported.

### Historical incidents

```cypher
MATCH p = (incident:PastIncident)-[:AFFECTED]->(service:Service)
WHERE service.name = $service_name
RETURN p,
       incident.id,
       incident.title,
       incident.root_cause,
       incident.resolution
LIMIT 20
```

### Service owners and runbooks

Owners and runbooks use separate queries so missing owners do not hide runbooks and vice versa:

```cypher
MATCH p = (service:Service)-[:MAINTAINED_BY]->(developer:Developer)
WHERE service.name = $service_name
RETURN p,
       developer.name,
       developer.email,
       developer.on_call
```

```cypher
MATCH p = (service:Service)-[:DOCUMENTED_IN]->(runbook:Runbook)
WHERE service.name = $service_name
RETURN p,
       runbook.title,
       runbook.url
```

All read-only tool queries use `graph.ro_query(...)`, bind `$service_name` as a parameter, and read rows through FalkorDB's `result.result_set`.

### Blast-radius algorithm

The deterministic blast-radius calculation:

1. Builds an in-memory reverse adjacency map from each dependency target to its direct dependents.
2. Starts at the selected service with distance zero.
3. Performs breadth-first traversal through incoming edges up to three hops.
4. Records each dependent service once at its shortest distance.
5. Counts distance `1` as direct and distances greater than `1` as indirect.
6. Uses the largest recorded shortest distance as the maximum distance.

This prevents multiple paths to one service from inflating the unique dependent count.

### Incident Rehearsal algorithm

For a selected incoming edge, the UI copies the current edge list and removes exactly that `(source, target)` tuple. It then runs the same blast-radius and comparison helpers on the simulated list. The database is never changed.

The comparison reports:

- Services present before but absent after: no longer reachable.
- Services present in both: remain affected.
- Services present only after: newly reachable.

## Installation and local setup

The commands below are written for Windows PowerShell.

### 1. Clone the repository

```powershell
git clone https://github.com/prithvi-yadav/Incident-Rehearsal-Explainable-AI-Blast-Radius-Investigator.git Incident-Rehearsal-Explainable-AI-Blast-Radius-Investigator
Set-Location .\Incident-Rehearsal-Explainable-AI-Blast-Radius-Investigator
```

The clone command and destination use the published repository:
`https://github.com/prithvi-yadav/Incident-Rehearsal-Explainable-AI-Blast-Radius-Investigator`.
The clone command was not run in this workspace.

### 2. Create and activate a virtual environment

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
```

If PowerShell execution policy prevents activation, use an approved local policy change or invoke the interpreter directly as shown below. The project was inspected with Python 3.13.15, but a fresh environment setup was not run as part of the documentation update.

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The manifest uses exact pins. The versions listed in the technology table are
the versions currently installed in the existing `venv`, matching the
published `requirements.txt`.

### 4. Configure FalkorDB

For local development, the application defaults to `localhost:6379` and the
`incident_investigator` graph. Copy `.env.example` to `.env` if you want to
override those defaults or configure authentication/TLS:

| Variable | Purpose |
|---|---|
| `FALKORDB_URL` | Optional `falkor://` or `falkors://` endpoint; preferred for hosted instances |
| `FALKORDB_HOST` / `FALKORDB_PORT` | Host and port fallback when `FALKORDB_URL` is not set |
| `FALKORDB_GRAPH_NAME` | Graph name; defaults to `incident_investigator` |
| `FALKORDB_USERNAME` / `FALKORDB_PASSWORD` | Optional authentication for explicit host/port configuration |
| `FALKORDB_SSL` | Enable TLS for explicit host/port configuration |
| `FALKORDB_SSL_CERT_REQS` | TLS certificate requirement, such as `required` |
| `FALKORDB_SSL_CA_CERTS` / `FALKORDB_SSL_CA_PATH` / `FALKORDB_SSL_CA_DATA` | Optional CA configuration |
| `FALKORDB_SSL_CERTFILE` / `FALKORDB_SSL_KEYFILE` | Optional client certificate and key |
| `FALKORDB_SSL_PASSWORD` | Optional client-key password |
| `FALKORDB_SSL_CHECK_HOSTNAME` | Enable TLS hostname checking |
| `FALKORDB_SSL_VALIDATE_OCSP` / `FALKORDB_SSL_VALIDATE_OCSP_STAPLED` | Optional OCSP validation |
| `FALKORDB_SSL_OCSP_EXPECTED_CERT` | Optional expected OCSP certificate |

The installed FalkorDB client supports these connection and TLS options. Do
not commit credentials or URLs containing credentials.

### 5. Start FalkorDB locally with Docker

A typical local FalkorDB container command is:

```powershell
docker run --name falkordb `
  -p 6379:6379 `
  -p 3000:3000 `
  -d falkordb/falkordb:latest
```

If the container already exists:

```powershell
docker start falkordb
```

The Docker commands were not executed during this documentation update. Confirm the image tag and local Docker installation before use.

### 6. Configure Gemini locally

Create a `.env` file in the project root from the tracked template. Do not
commit the local `.env`:

```powershell
Copy-Item .env.example .env
```

`agent.py` loads `GEMINI_API_KEY` first and falls back to `GOOGLE_API_KEY`
from the environment or `.env`. Never paste a real key into source code or
public documentation.

### 7. Seed the synthetic graph

```powershell
python .\seed_graph.py
```

This writes the synthetic service and evidence records to FalkorDB using `MERGE`. It was not run as part of the README update.

### 8. Run deterministic graph verification

The relationship-effect verification temporarily removes only `Checkout → AuthService`, checks the changed tool output, restores the relationship with `MERGE`, and verifies the restored paths:

```powershell
python .\verify_graph_effect.py
```

This script changes the local graph temporarily and restores the edge in its cleanup path. Run it only against the intended local graph. It was not run as part of the README update.

### 9. Start Streamlit

```powershell
python -m streamlit run app.py
```

Open the local URL printed by Streamlit. Starting the Streamlit server and browser verification were not performed during the README update.

### Streamlit Community Cloud configuration

Deploy the repository with `app.py` as the main file. In the app's
**Settings → Secrets** panel, add the following TOML configuration using your
actual hosted FalkorDB values:

```toml
GEMINI_API_KEY = "your-gemini-api-key-here"
FALKORDB_URL = "falkors://username:password@example.com:6379"
FALKORDB_GRAPH_NAME = "incident_investigator"
```

The application reads Streamlit secrets when available and keeps local
environment/`.env` configuration working for development. For a URL without
embedded credentials, provide `FALKORDB_USERNAME` and `FALKORDB_PASSWORD` as
separate secrets. TLS and certificate settings use the `FALKORDB_SSL_*`
variables documented above. Do not publish real secret values.

The remote FalkorDB endpoint must be reachable from Streamlit Community Cloud,
allow its outbound connection, and contain the expected graph data. The app
does not seed, update, delete, or otherwise modify the database at startup.

## Using the application

1. Open the Overview tab.
2. Describe the incident or use the default example.
3. Choose the focus service. The app can automatically focus a service mentioned in the incident description.
4. Review the live dependency graph and KPI cards.
5. Select **Investigate Incident** to run the Gemini agent. If Gemini quota exhaustion is detected, the deterministic fallback creates a graph-evidence report.
6. Open **Incident Rehearsal** to select the baseline or isolate an actual incoming edge.
7. Use **Compare All Scenarios** to rank every candidate edge by lost graph reachability.
8. Review Before/After Graphviz panels and the reachability comparison.
9. Open **Investigation Report** to read or download the complete Markdown report.

## Verification and testing

The following checks were confirmed in the existing virtual environment during project development:

```powershell
.\venv\Scripts\python.exe -m py_compile .\app.py .\agent.py .\tools.py
```

The deterministic rehearsal checks covered:

- `AuthService` baseline: 3 unique dependents, 2 direct, 1 indirect, maximum distance 2.
- Isolating `Checkout → AuthService`: 1 remaining unique dependent; `Checkout` and `MobileAPI` no longer reachable.
- Isolating `CartService → AuthService`: 2 remaining unique dependents; only `CartService` no longer reachable.
- Automatic scenario ranking, deterministic tie-breaking, and DOT edge removal.

The Python syntax checks and deterministic rehearsal checks above were run in
the existing virtual environment. The Streamlit browser experience was not
manually exercised during this documentation review, so no browser behavior is
claimed as tested here. A fresh dependency installation, Docker startup, and a
new Gemini API request were also not performed and are not claimed as tested.

## Evidence and safety boundaries

- The seeded records are synthetic demonstration data.
- A graph dependency path indicates potential structural exposure, not a confirmed outage.
- A recent deployment and historical incident are clues, not proof of current causality.
- Missing graph records are unknown information, not evidence that an event did not occur.
- Blast-radius counts do not represent business criticality.
- The Automatic Scenario Analyzer ranks graph reachability changes; it does not recommend a production change.
- Incident Rehearsal operates on an in-memory edge copy and does not validate whether isolation preserves production functionality.
- The application does not expose API keys in its user-facing error messages.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI, Graphviz rendering, blast-radius helpers, Incident Rehearsal, and scenario analyzer |
| `agent.py` | Gemini LangChain agent, evidence-grounded prompt, quota detection, and deterministic fallback report |
| `tools.py` | Shared FalkorDB connection, read-only Cypher queries, LangChain tools, and `incident_tools` |
| `seed_graph.py` | Idempotent synthetic service and operational-evidence seed script |
| `verify_graph_effect.py` | Temporary relationship deletion/restoration verification |
| `requirements.txt` | Python dependency constraints |
| `.env.example` | Safe template containing the required environment variable name |
| `.env` | Local secret configuration; ignored by Git and not committed |

## Limitations and future work

- The current graph schema is a demonstration model and is not connected to production telemetry.
- Incident descriptions are supplied by users and are not independently verified.
- The deterministic fallback preserves evidence but does not provide LLM-generated prioritization.
- The rehearsal simulator models dependency reachability only; it does not model retries, queues, traffic, health checks, or runtime service behavior.
- A production deployment would need stronger secret management, authentication, observability, and a controlled graph-data ingestion process.

## License

No license file is currently present in the project. Add an explicit open-source license before distributing the repository publicly.
