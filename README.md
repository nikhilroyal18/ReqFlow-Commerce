# ReqFlow Commerce

ReqFlow Commerce is an evidence-grounded requirements engineering workspace for e-commerce software projects. It gathers stakeholder input and project documents, drafts traceable requirements, flags quality issues, builds risk and traceability artefacts, and recommends a project-specific SDLC for human review.

## Workflow

1. Describe the e-commerce scope and complete an adaptive stakeholder interview.
2. Upload project documents such as product requirements, policies, interface specifications, meeting notes, and operational procedures.
3. Review retrieved evidence and generate requirement drafts.
4. Inspect quality findings, advisory security/privacy controls, risks, and traceability.
5. Review the ranked SDLC recommendation and workflow, then record a human decision.
6. Export the project audit trail as JSON.

The workflow is sequential. Requirement generation, compliance/risk analysis, documentation, and SDLC selection are implemented as separate service agents. Generated content remains advisory. The default control catalogue contains e-commerce review prompts, not regulations. Jurisdictional compliance mapping requires project-approved source documents and an authorised reviewer.

## Capabilities and boundaries

- FastAPI application with a responsive, browser-based workspace.
- Adaptive stakeholder interview with local fallback when no LLM is configured.
- `.txt`, `.md`, `.csv`, `.pdf`, and `.docx` source upload, sensitive-pattern masking, chunking, retrieval, and project-scoped ChromaDB indexing.
- Evidence-only local draft generation, or configured OpenAI-compatible generation with evidence validation.
- Rule-based requirement classification, ambiguity/quality checks, clarification queue, risk register, traceability, generated SRS/user stories/use cases, and evaluation measures.
- Transparent, deterministic SDLC rankings and project workflow. The scoring method is implemented in `app/services/sdlc.py`; unknown schedule/change facts remain explicitly unevidenced.
- Human approval status and project audit events.
- Saved project selection, project deletion, requirement editing/deletion, persisted clarification responses, SRS Markdown export, and requirement CSV export.
- Configurable local role-based access with session cookies, project ownership, and reviewer-only approval decisions when credentials are configured.
- Data persists as per-project JSON files in `APP_DATA_DIR` (default `data/`); vectors persist under `VECTOR_DB_PATH` (default `data/chroma`).

Local demo mode remains unauthenticated when no users are configured. To enable authentication, add these settings to `.env` and restart:

```text
APP_AUTH_REQUIRED=true
APP_USERS_JSON={"admin":{"password":"replace-with-a-long-secret","role":"admin"},"analyst":{"password":"another-long-secret","role":"analyst"},"reviewer":{"password":"review-secret","role":"reviewer"},"reader":{"password":"reader-secret","role":"viewer"}}
```

Roles: `admin` manages all projects, `analyst` creates and edits owned projects, `reviewer` records approval decisions for owned projects, and `viewer` can read owned projects. Session state is held in process memory and expires when the app restarts. Use HTTPS when exposing the app beyond localhost. The prototype still lacks MFA, password hashing/account lifecycle, durable session storage, rate limiting, encrypted durable storage, and a relational database. It is not production hardened. Uploaded material is project evidence and treated as untrusted prompt input. Review provider data handling before enabling an external LLM.

## Quick start

Requires Python 3.10 or later.

```powershell
python -m venv .venv
\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open <http://127.0.0.1:8000>. API docs: <http://127.0.0.1:8000/docs>.

## LLM configuration

The app runs without an API key using local embeddings and evidence-only drafts. Add a root `.env` file to enable an OpenAI-compatible provider:

```text
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
APP_DATA_DIR=data
VECTOR_DB_PATH=data/chroma
```

`OPENAI_API_KEY` is also accepted. Leave `EMBEDDING_MODEL` blank to use local embeddings. Keep secrets out of source control and exports.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/rag/status` | Safe provider and vector configuration status |
| POST | `/api/interview/next-question` | Select the next interview question |
| POST | `/api/projects` | Create and index a project |
| GET | `/api/projects/{id}/retrieval?query=...` | Retrieve project evidence |
| POST | `/api/projects/{id}/requirements` | Generate drafts and derived analysis |
| POST | `/api/projects/{id}/requirements/{requirement_id}/approval` | Record requirement review status |
| POST | `/api/projects/{id}/sdlc` | Generate SDLC recommendation |
| POST | `/api/projects/{id}/sdlc/approval` | Record the human SDLC decision |
| GET | `/api/projects/{id}/artefacts` | Retrieve generated documentation artefacts |
| GET | `/api/projects/{id}/evaluation` | Retrieve transparent prototype metrics |
| GET | `/api/projects/{id}/export` | Export project and audit trail |
| GET | `/api/projects` | List projects available to the signed-in user |
| PUT/DELETE | `/api/projects/{id}/requirements/{requirement_id}` | Edit or delete a requirement |
| POST | `/api/projects/{id}/clarifications` | Save a clarification response and index it as project evidence |
| GET | `/api/projects/{id}/documents/md` | Download SRS Markdown |
| GET | `/api/projects/{id}/documents/csv` | Download requirements CSV |
| GET/POST | `/api/auth/status`, `/api/auth/login` | Authentication status and login |
| POST | `/api/auth/logout` | End the local session |
| GET | `/api/knowledge-base/ecommerce` | Inspect advisory e-commerce review prompts |

## Requirements-to-implementation checklist

| Problem Statement objective (adapted) | Status | Evidence / limitation |
| --- | --- | --- |
| Multi-agent requirement, risk, documentation, and SDLC workflow | Partially implemented | Sequential requirement, control/risk, documentation, and SDLC agents in `app/services/agents.py`; coordinator, clarification, security, validation, and approval are not independent executable agents. |
| Stakeholder and document gathering with masking and retrieval | Implemented | Interview API, uploads, masking, ChromaDB retrieval. No email/transcript connectors. |
| Classification, ambiguity and quality checks, clarification queue | Partially implemented | Deterministic checks, persisted clarification answers, and evidence indexing; no robust cross-stakeholder contradiction model. |
| Evidence-grounded outputs and traceability | Implemented | Source chunks are linked to draft requirements and traceability records. LLM optional; confidence is heuristic, not calibrated. |
| E-commerce security/privacy and risk review | Partially implemented | Advisory keyword-driven control prompts and risk entries; no jurisdiction-specific legal catalogue or full threat model. |
| Explainable SDLC ranking and project workflow | Implemented | Explicit deterministic scoring and generated workflow; some factors are neutral defaults where evidence is absent. |
| Human review and audit trail | Partially implemented | Requirement/SDLC decisions, reviewer identity/role, configurable RBAC, owner isolation, and audit events persist; separate compliance approval and MFA are absent. |
| SRS, stories, use cases, risk, control and traceability artefacts | Partially implemented | Derived JSON, Markdown SRS, CSV requirements, and project JSON export; no DOCX/PDF export or in-app artefact editing. |
| Evaluation metrics | Partially implemented | Prototype coverage measures only; no labelled case studies, precision/recall, hallucination study, stakeholder satisfaction, or baseline comparison. |
| Secure production operation, access control, isolation | Partially implemented | Configurable local authentication, role checks, and project owner isolation; no MFA, durable sessions, encrypted storage, retention controls, rate limiting, or security audit. Local mode is unauthenticated unless configured. |

## Tests

```powershell
python -m pytest -q
```

Tests cover evidence-to-requirement generation, quality checks, SDLC ranking, risk/traceability artefacts, masking, provider fallback, requirement editing, clarification persistence, document exports, login roles, and project access checks. They are prototype unit/API tests, not full browser journeys, penetration testing, or production security validation.

## Sample input

Use [ sample_data/ecommerce_checkout_policy.txt ](SE_Nikhil/sample_data/ecommerce_checkout_policy.txt) for a short checkout, order, and customer-data example. The sample is illustrative project input, not legal advice or a claim of regulatory applicability.
