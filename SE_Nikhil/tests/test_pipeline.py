from app.schemas import Category
from app.services.documents import chunk_text, mask_sensitive, retrieve
from app.services.generator import generate_evidence_drafts
from app.services.quality import analyse
from io import BytesIO
from urllib.error import HTTPError

from app.services import rag
from app.services.rag import RagConfigurationError, VectorStore, _api_key, _embed, provider_configured
from app.services.sdlc import recommend
from app.services.agents import ComplianceRiskAgent, DocumentationAgent, GovernanceSdlcAgent, RequirementsAgent
from app.schemas import Evidence, Project, Requirement
from datetime import datetime


def test_pipeline_creates_grounded_requirements_and_recommendation():
    text = "The system shall encrypt personal data. The system must record audit events for every decision."
    chunks = [chunk.__dict__ for chunk in chunk_text(mask_sensitive(text))]
    documents = [{"id": "DOC-001", "name": "policy.txt", "chunks": chunks}]
    evidence = retrieve("personal data and audit", documents)

    requirements = generate_evidence_drafts("customer onboarding", evidence)

    assert requirements
    assert all(requirement.evidence for requirement in requirements)
    assert Category.SECURITY in requirements[0].categories or Category.PRIVACY in requirements[0].categories
    assert isinstance(analyse(requirements), list)
    recommendation = recommend(requirements)
    assert recommendation.options[0].score >= recommendation.options[-1].score
    assert {option.name for option in recommendation.options} == {"Waterfall", "V-Shape", "Prototyping", "RAD", "Spiral", "Incremental", "Agile", "DevSecOps", "Agileâ€“V-Model hybrid"}
    assert {factor.name for factor in recommendation.factors} == {"Requirement stability", "Requirement clarity", "Risk", "Complexity", "Need for early prototype", "Time constraints", "Customer involvement", "Frequency of changes", "Need for iterative development", "Need for risk analysis"}


def test_two_agents_have_a_sequential_hand_off():
    chunks = [chunk.__dict__ for chunk in chunk_text("The system shall encrypt personal data and record every decision.")]
    evidence = retrieve("encrypt and audit", [{"id": "DOC-001", "name": "policy.txt", "chunks": chunks}])
    project = Project(id="two-agent", functionality="Customer onboarding", created_at=datetime.now())

    requirements = RequirementsAgent().run(project, evidence)
    recommendation = GovernanceSdlcAgent().run(project)

    assert requirements == project.requirements
    assert project.quality_issues is not None
    assert recommendation == project.sdlc


def test_v_shape_wins_for_a_controlled_high_risk_project_with_formal_verification():
    requirement = Requirement(
        id="REQ-001",
        statement="The approved baseline must follow controlled change management and traceability between requirements and test results.",
        categories=[Category.SECURITY, Category.REGULATORY, Category.INTEGRATION],
        evidence=[Evidence(document_id="DOC-001", document_name="plan", chunk_id="chunk-001", excerpt="Requirements and acceptance criteria are formally approved before development. Unit testing, integration testing, system testing and acceptance testing are required at each stage.", relevance=1)],
        business_justification="Regulated lending delivery.",
        acceptance_criteria=["Each approved requirement has linked verification evidence."],
        risk_level="High",
        confidence=0.9,
        reasoning="Explicit staged verification evidence.",
        approval_status="Needs review",
    )

    recommendation = recommend([requirement])

    assert recommendation.recommended_sdlc == "V-Shape"


def test_governance_agent_uses_full_project_evidence_for_sdlc_signals():
    chunks = [chunk.__dict__ for chunk in chunk_text("Requirements and acceptance criteria are formally approved before development. Unit testing, integration testing, system testing and acceptance testing are required at each stage. Controlled change management is mandatory.")]
    project = Project(
        id="v-model-context",
        functionality="Regulated lending",
        created_at=datetime.now(),
        documents=[{"id": "DOC-001", "name": "interview", "chunks": chunks}],
    )
    project.requirements = [Requirement(
        id="REQ-001", statement="The system must apply regulated lending controls.", categories=[Category.REGULATORY, Category.SECURITY],
        evidence=[], business_justification="Compliance", acceptance_criteria=[], risk_level="High", confidence=0.9,
        reasoning="Stakeholder evidence", approval_status="Needs review",
    )]

    recommendation = GovernanceSdlcAgent().run(project)

    assert recommendation.recommended_sdlc == "V-Shape"


def test_ecommerce_controls_risks_traceability_and_artefacts_are_reviewable():
    chunks = [chunk.__dict__ for chunk in chunk_text("The system shall verify customer identity for customer identity and retain an audit record for every decision.")]
    evidence = retrieve("customer identity audit", [{"id": "DOC-001", "name": "policy.txt", "chunks": chunks}])
    project = Project(id="commerce", functionality="Storefront onboarding", created_at=datetime.now())

    RequirementsAgent().run(project, evidence)
    ComplianceRiskAgent().run(project)
    DocumentationAgent().run(project)

    assert any(mapping.control_id == "ECOM-IDENTITY" for requirement in project.requirements for mapping in requirement.compliance_mappings)
    assert project.risks
    assert project.traceability
    assert {"srs", "user_stories", "use_cases", "risk_register", "traceability_matrix", "compliance_control_matrix"} <= set(project.artefacts)
    assert project.clarification_questions
    assert project.evaluation is not None
    assert project.evaluation.citation_coverage == 1


def test_masking_replaces_account_like_values():
    assert "1234567890123456" not in mask_sensitive("Account 1234567890123456 must be protected.")


def test_rag_requires_explicit_provider_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    try:
        _api_key()
    except RagConfigurationError:
        return
    raise AssertionError("RAG must not run without an embedding-provider key")


def test_rag_configuration_never_exposes_api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "not-for-the-client")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    configuration = VectorStore.configuration()

    assert configuration["vector_database"] == "ChromaDB"
    assert configuration["llm_model"] == "test-model"
    assert "not-for-the-client" not in str(configuration)


def test_rag_uses_local_embeddings_without_provider_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    vectors = _embed(["identity verification requires an audit record"])

    assert len(vectors) == 1
    assert len(vectors[0]) == 384
    assert any(vectors[0])


def test_rag_falls_back_to_local_embeddings_when_provider_rejects_request(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    def rejected_request(*_args, **_kwargs):
        raise HTTPError("https://example.invalid/v1/embeddings", 404, "Not Found", {}, BytesIO())

    monkeypatch.setattr(rag, "urlopen", rejected_request)

    vectors = rag._embed(["Identity verification requires an audit record."])

    assert len(vectors) == 1
    assert len(vectors[0]) == 384
    assert any(vectors[0])


def test_openai_api_key_environment_variable_is_supported(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert provider_configured()
    assert _api_key() == "test-key"


def test_project_creation_works_without_provider_key(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.services.storage import ProjectStore

    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "projects"))
    from app import main

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(main, "vector_store", VectorStore())
    monkeypatch.setattr(main, "store", ProjectStore())
    client = TestClient(main.app)
    response = client.post(
        "/api/projects",
        data={"functionality": "Digital customer onboarding", "stakeholder_input": '{"identity": "Verify identity and record each decision."}'},
    )

    assert response.status_code == 200
    assert response.json()["rag"]["vector_database"] == "ChromaDB"
    knowledge_sources = [item for item in response.json()["documents"] if item["source_type"] == "Approved project reference"]
    assert len(knowledge_sources) == 0


def test_interview_question_endpoint_uses_llm_agent(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        main,
        "select_interview_question",
        lambda functionality, answers, candidates: {
            "key": "exceptions",
            "topic": "Exceptions and overrides",
            "prompt": "What happens when checkout payment fails or needs an authorised override?",
        },
    )
    response = TestClient(main.app).post(
        "/api/interview/next-question",
        json={
            "functionality": "Online checkout",
            "answers": {"business objective": "Process customer orders securely."},
            "asked_question_keys": ["workflow and outcomes"],
        },
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "LLM adaptive agent"
    assert response.json()["key"] == "exceptions"


def test_project_requirement_clarification_and_document_exports(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    from app.services.storage import ProjectStore

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "vectors"))
    monkeypatch.setattr(main, "store", ProjectStore())
    monkeypatch.setattr(main, "vector_store", VectorStore())
    project = Project(id="commerce-crud", functionality="Marketplace checkout", created_at=datetime.now())
    project.requirements = [Requirement(
        id="REQ-001", statement="The checkout shall show the order total before payment.", categories=[Category.FUNCTIONAL],
        evidence=[], business_justification="Customers need to review the purchase total.", acceptance_criteria=["Total is visible before payment."],
        confidence=0.7, reasoning="Stakeholder input", approval_status="Approved",
    )]
    main.store.save(project)
    chunks = [chunk.__dict__ for chunk in chunk_text("The checkout shall show the order total before payment.")]
    main.vector_store.index(project.id, [{"id": "DOC-001", "name": "interview", "chunks": chunks}])
    client = TestClient(main.app)

    response = client.put("/api/projects/commerce-crud/requirements/REQ-001", json={
        "statement": "The checkout shall display a full order total before payment submission.",
        "business_justification": "Customers must review the complete purchase amount.",
        "acceptance_criteria": ["The total is visible before submitting payment."], "priority": "Must", "risk_level": "Low",
    })
    assert response.status_code == 200
    assert response.json()["approval_status"] == "Needs review"

    response = client.post("/api/projects/commerce-crud/clarifications", json={
        "question": "REQ-001: Which charges are included in the total?", "response": "Items, tax, shipping, and discounts.", "requirement_id": "REQ-001",
    })
    assert response.status_code == 200
    assert response.json()["question"] == "REQ-001: Which charges are included in the total?"
    assert len(main.store.get("commerce-crud").clarification_responses) == 1

    exported = client.get("/api/projects/commerce-crud/documents/csv")
    assert exported.status_code == 200
    assert "REQ-001" in exported.text and "order total" in exported.text
    assert client.get("/api/projects/commerce-crud/documents/md").status_code == 200
    assert len(client.get("/api/projects").json()) == 1


def test_configured_authentication_enforces_roles_and_project_isolation(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    from app.services.storage import ProjectStore

    monkeypatch.setenv("APP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("APP_USERS_JSON", '{"analyst":{"password":"analyst-secret","role":"analyst"},"reviewer":{"password":"review-secret","role":"reviewer"},"viewer":{"password":"viewer-secret","role":"viewer"},"admin":{"password":"admin-secret","role":"admin"}}')
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(main, "store", ProjectStore())
    main.store.save(Project(id="owned-project", functionality="Seller catalogue", owner_user="analyst", created_at=datetime.now()))
    client = TestClient(main.app)

    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/login", json={"username": "analyst", "password": "analyst-secret"}).status_code == 200
    assert client.get("/api/projects").status_code == 200
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "viewer", "password": "viewer-secret"}).status_code == 200
    assert client.put("/api/projects/not-a-project/requirements/REQ-001", json={}).status_code == 403
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "reviewer", "password": "review-secret"}).status_code == 200
    assert client.post("/api/projects/not-a-project/requirements/REQ-001/approval", data={"status": "Approved"}).status_code == 404
    assert client.get("/api/projects/owned-project").status_code == 404




