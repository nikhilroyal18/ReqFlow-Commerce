"""Evidence-led e-commerce security, privacy, and operational control review.

The catalogue contains review prompts, not regulatory claims. A project must
provide its own approved policies and jurisdictional sources for compliance
mapping; uncertain applicability stays with a human reviewer.
"""
from __future__ import annotations

from app.schemas import Category, ComplianceMapping, Requirement, RiskEntry, TraceabilityRecord

CONTROLS = [
    {"id": "ECOM-IDENTITY", "title": "Customer and staff identity controls", "terms": ("authentication", "login", "account", "identity", "session"), "control": "Review authentication, account recovery, session handling, and staff access boundaries."},
    {"id": "ECOM-PAYMENT", "title": "Payment and checkout security review", "terms": ("payment", "checkout", "card", "gateway", "transaction", "refund"), "control": "Review payment-provider boundaries, checkout integrity, sensitive-data handling, and failure recovery."},
    {"id": "ECOM-PRIVACY", "title": "Customer data privacy review", "terms": ("personal data", "customer data", "privacy", "consent", "retention", "profile"), "control": "Confirm data purpose, minimisation, access, retention, and deletion against an approved project policy."},
    {"id": "ECOM-ORDER-AUDIT", "title": "Order and administrative auditability", "terms": ("order", "inventory", "refund", "cancel", "audit", "log"), "control": "Define traceable order-state changes, privileged actions, and operational evidence."},
    {"id": "ECOM-API-RESILIENCE", "title": "Marketplace and integration resilience", "terms": ("api", "integration", "seller", "shipment", "warehouse", "carrier"), "control": "Review interface ownership, validation, retries, idempotency, and failure handling."},
]


def catalogue() -> list[dict]:
    return [{"id": c["id"], "title": c["title"], "version": "Advisory catalogue v1", "applicability": "Review against project-approved policy and applicable jurisdictional sources.", "control": c["control"], "source_url": None} for c in CONTROLS]


def map_requirement(requirement: Requirement) -> list[ComplianceMapping]:
    text = " ".join([requirement.statement, *(c.value for c in requirement.categories)]).lower()
    return [ComplianceMapping(control_id=c["id"], source_title=f"{c['title']} (advisory practice; not a regulation)", source_url="", jurisdiction="Project-defined", applicability="Review with the project security/privacy owner; no legal applicability is asserted.", rationale=c["control"]) for c in CONTROLS if any(term in text for term in c["terms"])]


def analyse(requirements: list[Requirement]) -> tuple[list[RiskEntry], list[TraceabilityRecord]]:
    risks: list[RiskEntry] = []
    traceability: list[TraceabilityRecord] = []
    risk_categories = {
        Category.SECURITY: ("Security", "High", "Security controls need review by the accountable security owner."),
        Category.PRIVACY: ("Privacy", "High", "Customer-data handling needs review against approved policy and applicable law."),
        Category.REGULATORY: ("Compliance", "High", "Applicability must be supported by an approved source and confirmed by an authorised reviewer."),
        Category.AUDIT: ("Operational", "Medium", "Audit evidence and retention need an accountable owner and defined policy."),
        Category.INTEGRATION: ("Integration", "Medium", "External services need resilience, failure-handling, and ownership review."),
    }
    for requirement in requirements:
        requirement.compliance_mappings = map_requirement(requirement)
        if requirement.compliance_mappings and not requirement.applicable_policy:
            requirement.applicable_policy = "; ".join(m.control_id for m in requirement.compliance_mappings)
        for category in requirement.categories:
            if category not in risk_categories:
                continue
            kind, level, rationale = risk_categories[category]
            risk_id = f"RISK-{len(risks) + 1:03d}"
            risks.append(RiskEntry(id=risk_id, requirement_id=requirement.id, title=f"{kind} risk for {requirement.id}", category=kind, level=level, rationale=rationale, mitigation="Define a measurable control, test evidence, accountable owner, and approval gate.", owner_role="Privacy / compliance reviewer" if kind in {"Compliance", "Privacy"} else "Security / technical owner"))
        for evidence in requirement.evidence:
            traceability.append(TraceabilityRecord(requirement_id=requirement.id, source_document=evidence.document_name, source_chunk_id=evidence.chunk_id, control_ids=[m.control_id for m in requirement.compliance_mappings], risk_ids=[r.id for r in risks if r.requirement_id == requirement.id]))
    return risks, traceability
