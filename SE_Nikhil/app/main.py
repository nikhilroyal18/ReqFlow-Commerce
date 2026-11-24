from __future__ import annotations

import json
import csv
import hmac
import secrets
from io import StringIO
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.schemas import AgentRun, ApprovalDecision, ClarificationResponse, InterviewQuestionRequest, InterviewQuestionResponse, LoginRequest, Project, RequirementUpdate
from app.services.agents import ComplianceRiskAgent, DocumentationAgent, GovernanceSdlcAgent, RequirementsAgent
from app.services.compliance import catalogue as ecommerce_catalogue
from app.services.documents import chunk_text, extract_text, mask_sensitive
from app.services.generator import generate_evidence_drafts
from app.services.knowledge import approved_documents
from app.services.quality import analyse as analyse_requirements, clarification_questions
from app.services.llm import enabled as llm_enabled, generate_requirements as generate_llm_requirements, select_interview_question
from app.services.rag import RagConfigurationError, VectorStore, embedding_provider_configured, provider_configured
from app.services.storage import ProjectStore

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
store = ProjectStore()
vector_store = VectorStore()
requirements_agent = RequirementsAgent()
compliance_risk_agent = ComplianceRiskAgent()
documentation_agent = DocumentationAgent()
governance_sdlc_agent = GovernanceSdlcAgent()

INTERVIEW_TOPICS = [
    {"key": "workflow and outcomes", "topic": "Workflow and outcomes"},
    {"key": "business rules", "topic": "Decision rules"},
    {"key": "exceptions", "topic": "Exceptions and overrides"},
    {"key": "data and integrations", "topic": "Data and integrations"},
    {"key": "security and compliance", "topic": "Security and compliance"},
    {"key": "service levels and scale", "topic": "Operational scale"},
    {"key": "delivery constraints", "topic": "Delivery context"},
    {"key": "validation and rollout", "topic": "Validation and rollout"},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    print("\n" + "=" * 56)
    print("  ReqFlow Commerce is running")
    print("  App:      http://127.0.0.1:8000")
    print("  API docs: http://127.0.0.1:8000/docs")
    print("  RAG:      ChromaDB + OpenAI-compatible embeddings")
    print("  Storage:  local JSON project data and ChromaDB vectors")
    print("  Stop:     press Ctrl+C")
    print("=" * 56 + "\n")
    yield
    print("ReqFlow Commerce stopped.")


app = FastAPI(title="ReqFlow Commerce", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
sessions: dict[str, dict[str, str]] = {}


def configured_users() -> dict[str, dict[str, str]]:
    raw = os.getenv("APP_USERS_JSON", "")
    if not raw:
        return {}
    try:
        users = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("APP_USERS_JSON must be valid JSON.") from exc
    if not isinstance(users, dict) or any(not isinstance(v, dict) or not v.get("password") or v.get("role") not in {"admin", "analyst", "reviewer", "viewer"} for v in users.values()):
        raise RuntimeError("Each APP_USERS_JSON user needs a password and role: admin, analyst, reviewer, or viewer.")
    return users


@app.middleware("http")
async def protect_api(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api") or path in {"/api/auth/status", "/api/auth/login", "/api/rag/status"}:
        return await call_next(request)
    users = configured_users()
    auth_required = os.getenv("APP_AUTH_REQUIRED", "true" if users else "false").lower() == "true"
    if not auth_required:
        request.state.user = {"username": "local-admin", "role": "admin"}
        return await call_next(request)
    user = sessions.get(request.cookies.get("reqflow_session", ""))
    if not user:
        return JSONResponse({"detail": "Sign in to use this workspace."}, status_code=401)
    request.state.user = user
    method = request.method.upper()
    role = user["role"]
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        if path.endswith("/approval"):
            allowed = role in {"admin", "reviewer"}
        elif method == "DELETE" and path.count("/") == 3:
            allowed = role == "admin"
        else:
            allowed = role in {"admin", "analyst"}
        if not allowed:
            return JSONResponse({"detail": "Your role cannot perform this action."}, status_code=403)
    parts = path.split("/")
    if len(parts) > 3 and parts[2] == "projects":
        try:
            project = store.get(parts[3])
        except KeyError:
            return JSONResponse({"detail": "Project not found."}, status_code=404)
        if role != "admin" and project.owner_user != user["username"]:
            return JSONResponse({"detail": "This project is not available to your account."}, status_code=404)
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status(request: Request) -> JSONResponse:
    users = configured_users()
    required = os.getenv("APP_AUTH_REQUIRED", "true" if users else "false").lower() == "true"
    user = sessions.get(request.cookies.get("reqflow_session", ""))
    return JSONResponse({"required": required, "configured_users": len(users), "user": user})


@app.post("/api/auth/login")
def auth_login(request: Request, credentials: LoginRequest) -> JSONResponse:
    users = configured_users()
    if not users:
        raise HTTPException(503, "Authentication is required but APP_USERS_JSON has no configured accounts.")
    account = users.get(credentials.username)
    if not account or not hmac.compare_digest(str(account["password"]), credentials.password):
        raise HTTPException(401, "Username or password was not accepted.")
    token = secrets.token_urlsafe(32)
    sessions[token] = {"username": credentials.username, "role": account["role"]}
    result = JSONResponse({"username": credentials.username, "role": account["role"]})
    result.set_cookie("reqflow_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=28_800)
    return result


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    sessions.pop(request.cookies.get("reqflow_session", ""), None)
    result = JSONResponse({"logged_out": True})
    result.delete_cookie("reqflow_session")
    return result


def audit(project: Project, action: str, detail: str) -> None:
    project.audit_log.append({"at": datetime.now(timezone.utc).isoformat(), "action": action, "detail": detail})


def record_agent_run(project: Project, agent: str, responsibility: str, status: str, output: str) -> None:
    project.agent_runs.append(AgentRun(
        agent=agent, responsibility=responsibility, status=status, output=output,
        at=datetime.now(timezone.utc),
    ))
    audit(project, "agent_completed", f"{agent}: {output}")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Serve an icon so browsers do not report a harmless missing-favicon error."""
    return FileResponse(ROOT / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/rag/status")
def rag_status() -> JSONResponse:
    return JSONResponse({
        **vector_store.configuration(),
        "embedding_provider_configured": embedding_provider_configured(),
        "generation_provider_configured": provider_configured(),
        "generation_mode": "LLM generation with citation validation" if llm_enabled() else "Evidence-only drafts until LLM_API_KEY is configured",
    })


@app.get("/api/knowledge-base/ecommerce")
def ecommerce_knowledge_base() -> JSONResponse:
    """Return advisory controls; jurisdictional applicability is never assumed."""
    return JSONResponse({"domain": "E-commerce", "controls": ecommerce_catalogue(), "notice": "These are review prompts, not regulatory determinations. Add project-approved sources and confirm applicability with authorised reviewers."})


@app.post("/api/interview/next-question", response_model=InterviewQuestionResponse)
def next_interview_question(request: InterviewQuestionRequest) -> InterviewQuestionResponse:
    """Use the configured LLM to select the next missing topic, with local fallback."""
    asked = set(request.asked_question_keys)
    candidates = [topic for topic in INTERVIEW_TOPICS if topic["key"] not in asked]
    if not candidates:
        return InterviewQuestionResponse(mode="Local fallback", completed=True)
    answers = {key: value.strip()[:4_000] for key, value in request.answers.items() if value.strip()}
    if llm_enabled():
        try:
            question = select_interview_question(request.functionality.strip(), answers, candidates)
            return InterviewQuestionResponse(**question, mode="LLM adaptive agent")
        except RuntimeError as exc:
            print("========== LLM ERROR ==========")
            print(repr(exc))
            print("================================")
            raise HTTPException(
                503,
                f"LLM error: {str(exc)}",
            ) from exc
    fallback = candidates[0]
    return InterviewQuestionResponse(
        **fallback,
        prompt=f"To complete the project picture, describe the {fallback['topic'].lower()} for this solution.",
        mode="Local fallback",
    )


@app.post("/api/projects")
async def create_project(
    request: Request,
    functionality: str = Form(...),
    stakeholder_input: str = Form("{}"),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    functionality = functionality.strip()
    if len(functionality) < 3:
        raise HTTPException(422, "Enter an e-commerce project scope of at least three characters.")
    try:
        answers = json.loads(stakeholder_input)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "Stakeholder answers could not be processed.") from exc
    if not isinstance(answers, dict):
        raise HTTPException(422, "Stakeholder answers must be a structured set of responses.")
    answers = {str(question): str(answer).strip() for question, answer in answers.items() if str(answer).strip()}
    uploads = [upload for upload in files if upload.filename]
    if not answers and not uploads:
        raise HTTPException(422, "Answer at least one stakeholder question or upload a supporting document.")
    # Index project-supplied evidence only; never assume a jurisdiction or policy.
    documents = approved_documents()
    knowledge_document_count = len(documents)
    project_source_count = 0
    if answers:
        interview_text = "\n".join(f"{question}: {answer}" for question, answer in answers.items())
        masked_text = mask_sensitive(interview_text)
        documents.append({"id": "DOC-001", "name": "Stakeholder interview", "chunks": [chunk.__dict__ for chunk in chunk_text(masked_text)]})
        project_source_count += 1
    for upload in uploads:
        content = await upload.read()
        if len(content) > 5_000_000:
            raise HTTPException(413, f"{upload.filename} exceeds the 5 MB prototype limit.")
        try:
            text = mask_sensitive(extract_text(upload.filename, content))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not text.strip():
            raise HTTPException(422, f"{upload.filename} contains no extractable text.")
        doc_id = f"DOC-{project_source_count + 1:03d}"
        documents.append({"id": doc_id, "name": upload.filename, "chunks": [chunk.__dict__ for chunk in chunk_text(text)]})
        project_source_count += 1
    user = getattr(request.state, "user", {"username": "local-admin"})
    project = Project(id=uuid4().hex[:12], functionality=functionality, owner_user=user["username"], created_at=datetime.now(timezone.utc), documents=documents)
    try:
        indexed_chunks = vector_store.index(project.id, documents)
    except RagConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    audit(project, "sources_recorded", f"Indexed {knowledge_document_count} approved reference source(s), plus {len(answers)} masked stakeholder response(s) and {len(uploads)} uploaded document(s).")
    audit(project, "rag_indexed", f"Indexed {indexed_chunks} source chunk(s) in ChromaDB.")
    store.save(project)
    return JSONResponse({
        "project_id": project.id,
        "documents": [{"id": doc["id"], "name": doc["name"], "chunks": len(doc["chunks"]), "source_type": "Approved project reference" if doc["id"].startswith("KB-") else "Project source", "version": doc.get("version"), "source_url": doc.get("source_url")} for doc in documents],
        "rag": {**vector_store.configuration(), "indexed_chunks": indexed_chunks},
    })


@app.get("/api/projects")
def list_projects(request: Request) -> JSONResponse:
    user = getattr(request.state, "user", {"username": "local-admin", "role": "admin"})
    projects = store.list()
    if user["role"] != "admin":
        projects = [p for p in projects if p.owner_user == user["username"]]
    return JSONResponse([{"id": p.id, "functionality": p.functionality, "created_at": p.created_at.isoformat(), "requirements": len(p.requirements), "pending_approval": sum(r.approval_status not in {"Approved", "Rejected"} for r in p.requirements), "high_risks": sum(r.level == "High" and r.status == "Open" for r in p.risks)} for p in projects])


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> JSONResponse:
    try:
        store.delete(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    vector_store.delete_project(project_id)
    return JSONResponse({"deleted": project_id})


@app.put("/api/projects/{project_id}/requirements/{requirement_id}")
def edit_requirement(request: Request, project_id: str, requirement_id: str, update: RequirementUpdate) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    requirement = next((r for r in project.requirements if r.id == requirement_id), None)
    if requirement is None:
        raise HTTPException(404, "Requirement not found.")
    if requirement.approval_status != "Needs review":
        requirement.approval_status = "Needs review"
        audit(project, "approved_requirement_changed", f"{requirement_id} changed and returned to review.")
    for key, value in update.model_dump().items():
        setattr(requirement, key, value)
    project.quality_issues = analyse_requirements(project.requirements)
    project.clarification_questions = clarification_questions(project.quality_issues)
    compliance_risk_agent.run(project)
    project.sdlc = None
    documentation_agent.run(project)
    audit(project, "requirement_edited", f"{requirement_id} edited by {request.state.user['username']} ({request.state.user['role']}).")
    store.save(project)
    return JSONResponse(requirement.model_dump(mode="json"))


@app.delete("/api/projects/{project_id}/requirements/{requirement_id}")
def delete_requirement(request: Request, project_id: str, requirement_id: str) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    before = len(project.requirements)
    project.requirements = [r for r in project.requirements if r.id != requirement_id]
    if len(project.requirements) == before:
        raise HTTPException(404, "Requirement not found.")
    project.quality_issues = analyse_requirements(project.requirements)
    project.clarification_questions = clarification_questions(project.quality_issues)
    compliance_risk_agent.run(project)
    project.sdlc = None
    documentation_agent.run(project)
    audit(project, "requirement_deleted", f"{requirement_id} deleted by {request.state.user['username']} ({request.state.user['role']}); prior approvals remain in the audit record.")
    store.save(project)
    return JSONResponse({"deleted": requirement_id})


@app.post("/api/projects/{project_id}/clarifications")
def save_clarification(request: Request, project_id: str, entry: ClarificationResponse) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    if entry.requirement_id and not any(r.id == entry.requirement_id for r in project.requirements):
        raise HTTPException(404, "Requirement not found.")
    record = {**entry.model_dump(), "at": datetime.now(timezone.utc).isoformat()}
    project.clarification_responses.append(record)
    project.clarification_questions = [q for q in project.clarification_questions if q != entry.question]
    doc_id = f"DOC-{len(project.documents) + 1:03d}"
    text = mask_sensitive(f"Clarification response for {entry.requirement_id or 'project'}: {entry.question} Answer: {entry.response}")
    project.documents.append({"id": doc_id, "name": f"Clarification {doc_id}", "chunks": [c.__dict__ for c in chunk_text(text)]})
    vector_store.index(project.id, project.documents)
    audit(project, "clarification_answered", f"Clarification saved for {entry.requirement_id or 'project'} by {request.state.user['username']}.")
    store.save(project)
    return JSONResponse(record)


@app.get("/api/projects/{project_id}/retrieval")
def retrieve_evidence(project_id: str, query: str | None = None) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    search_query = (query or project.functionality).strip()
    if len(search_query) < 3:
        raise HTTPException(422, "Enter a retrieval query of at least three characters.")
    try:
        evidence = vector_store.retrieve(project.id, search_query)
    except RagConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    return JSONResponse({"query": search_query, "matches": evidence, "count": len(evidence)})


@app.post("/api/projects/{project_id}/requirements")
def generate_requirements(project_id: str) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    try:
        evidence = vector_store.retrieve(project.id, project.functionality)
    except RagConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not evidence:
        raise HTTPException(422, "No retrievable evidence was found in the uploaded documents.")
    generation_mode = "evidence-only"
    if llm_enabled():
        try:
            generated_requirements = generate_llm_requirements(project.functionality, evidence)
            generation_mode = "LLM-assisted"
        except (RuntimeError, ValueError) as exc:
            generated_requirements = generate_evidence_drafts(project.functionality, evidence)
            audit(project, "llm_fallback", str(exc))
    else:
        generated_requirements = generate_evidence_drafts(project.functionality, evidence)
    project.requirements = requirements_agent.run(project, evidence, generated_requirements)
    record_agent_run(
        project, requirements_agent.name, requirements_agent.responsibility, "Waiting for human review",
        f"Generated {len(project.requirements)} {generation_mode} RAG-grounded draft requirement(s) and found {len(project.quality_issues)} quality issue(s).",
    )
    compliance_risk_agent.run(project)
    record_agent_run(
        project, compliance_risk_agent.name, compliance_risk_agent.responsibility, "Waiting for human review",
        f"Raised {sum(len(item.compliance_mappings) for item in project.requirements)} advisory control review prompt(s) and {len(project.risks)} reviewable risk entr{'y' if len(project.risks) == 1 else 'ies'}.",
    )
    documentation_agent.run(project)
    record_agent_run(
        project, documentation_agent.name, documentation_agent.responsibility, "Waiting for human review",
        "Generated exportable SRS, user-story, use-case, risk, compliance, traceability, and open-issue artefacts.",
    )
    store.save(project)
    return JSONResponse(project.model_dump(mode="json"))


@app.post("/api/projects/{project_id}/requirements/{requirement_id}/approval")
def update_approval(request: Request, project_id: str, requirement_id: str, status: str = Form(...)) -> JSONResponse:
    if status not in {"Approved", "Rejected", "Needs review", "Draft"}:
        raise HTTPException(422, "Invalid approval status.")
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    requirement = next((item for item in project.requirements if item.id == requirement_id), None)
    if not requirement:
        raise HTTPException(404, "Requirement not found.")
    requirement.approval_status = status
    if status != "Draft":
        reviewer = request.state.user
        project.approvals.append(ApprovalDecision(
            id=f"APR-{uuid4().hex[:8].upper()}", subject_type="Requirement", subject_id=requirement_id,
            decision=status, reviewer=reviewer["username"], reviewer_role=reviewer["role"],
            rationale="Requirement review status updated in the prototype interface.", at=datetime.now(timezone.utc),
        ))
    documentation_agent.run(project)
    audit(project, "requirement_approval_updated", f"{requirement_id} marked {status}.")
    store.save(project)
    return JSONResponse(requirement.model_dump(mode="json"))


@app.post("/api/projects/{project_id}/sdlc")
def sdlc_analysis(project_id: str) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    if not project.requirements:
        raise HTTPException(422, "Generate requirements before SDLC analysis.")
    project.sdlc = governance_sdlc_agent.run(project)
    documentation_agent.run(project)
    record_agent_run(
        project, governance_sdlc_agent.name, governance_sdlc_agent.responsibility, "Waiting for human review",
        f"Recommended {project.sdlc.recommended_sdlc}; human approval remains required.",
    )
    store.save(project)
    return JSONResponse(project.sdlc.model_dump(mode="json"))


@app.post("/api/projects/{project_id}/sdlc/approval")
def update_sdlc_approval(request: Request, project_id: str, status: str = Form(...), rationale: str = Form("")) -> JSONResponse:
    """Record the mandatory human decision on an advisory SDLC recommendation."""
    if status not in {"Approved", "Rejected", "Needs review"}:
        raise HTTPException(422, "Invalid SDLC approval status.")
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    if not project.sdlc:
        raise HTTPException(422, "Run SDLC analysis before recording its approval.")
    reviewer = request.state.user
    project.approvals.append(ApprovalDecision(
        id=f"APR-{uuid4().hex[:8].upper()}", subject_type="SDLC", subject_id=project.sdlc.recommended_sdlc,
        decision=status, reviewer=reviewer["username"], reviewer_role=reviewer["role"],
        rationale=rationale.strip() or "SDLC decision recorded in the prototype interface.", at=datetime.now(timezone.utc),
    ))
    documentation_agent.run(project)
    audit(project, "sdlc_approval_updated", f"{project.sdlc.recommended_sdlc} marked {status}.")
    store.save(project)
    return JSONResponse(project.approvals[-1].model_dump(mode="json"))


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> JSONResponse:
    try:
        return JSONResponse(store.get(project_id).model_dump(mode="json"))
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc


@app.get("/api/projects/{project_id}/artefacts")
def get_artefacts(project_id: str) -> JSONResponse:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    if not project.artefacts:
        raise HTTPException(422, "Generate requirements before requesting derived artefacts.")
    return JSONResponse(project.artefacts)


@app.get("/api/projects/{project_id}/evaluation")
def get_evaluation(project_id: str) -> JSONResponse:
    """Return transparent, prototype-level effectiveness metrics for demonstration."""
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    if not project.evaluation:
        raise HTTPException(422, "Generate requirements before requesting evaluation metrics.")
    return JSONResponse(project.evaluation.model_dump(mode="json"))


@app.get("/api/projects/{project_id}/documents/{format}")
def export_document(project_id: str, format: str) -> Response:
    try:
        project = store.get(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc
    if not project.requirements:
        raise HTTPException(422, "Generate requirements before exporting documents.")
    if format == "md":
        lines = [f"# Software Requirements Specification: {project.functionality}", "", "Generated from project evidence; human approval is required before baselining.", ""]
        for item in project.requirements:
            lines.extend([f"## {item.id} — {item.statement}", "", f"- **Category:** {', '.join(c.value for c in item.categories)}", f"- **Priority:** {item.priority}", f"- **Status:** {item.approval_status}", f"- **Risk:** {item.risk_level}", f"- **Rationale:** {item.business_justification}", "", "### Acceptance criteria", *[f"- {criterion}" for criterion in item.acceptance_criteria], "", "### Sources", *[f"- {e.document_name} ({e.chunk_id}): {e.excerpt}" for e in item.evidence], ""])
        lines.extend(["## Risk register", "", *[f"- **{r.id} / {r.level}:** {r.title}. {r.mitigation}" for r in project.risks]])
        lines.extend(["", "## Traceability", "", *[f"- {t.requirement_id} ← {t.source_document} ({t.source_chunk_id})" for t in project.traceability]])
        return Response("\n".join(lines), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="reqflow-{project_id}-srs.md"'})
    if format == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["Requirement ID", "Statement", "Category", "Priority", "Risk", "Status", "Acceptance criteria", "Sources"])
        for item in project.requirements:
            writer.writerow([item.id, item.statement, "; ".join(c.value for c in item.categories), item.priority, item.risk_level, item.approval_status, " | ".join(item.acceptance_criteria), " | ".join(f"{e.document_name}:{e.chunk_id}" for e in item.evidence)])
        return Response(stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="reqflow-{project_id}-requirements.csv"'})
    raise HTTPException(422, "Supported document formats are md and csv.")


@app.get("/api/projects/{project_id}/export")
def export_project(project_id: str) -> JSONResponse:
    try:
        return JSONResponse(store.export(store.get(project_id)), headers={"Content-Disposition": f'attachment; filename="reqflow-{project_id}.json"'})
    except KeyError as exc:
        raise HTTPException(404, "Project not found.") from exc


