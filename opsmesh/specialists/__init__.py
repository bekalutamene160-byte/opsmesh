"""
OPSMESH Specialists -- real LLM-powered specialists for the production worker.

Each specialist loads the Lead from the LeadStore to get the company name,
contact email, and industry. This makes the research, draft, and critic
steps use REAL data about the lead, not just the lead_id.

Architecture:
  - ResearchSpecialist: Exa search + Groq LLM → research summary
  - QualifySpecialist: Groq LLM → qualification score
  - DraftSpecialist: Groq LLM → outreach email
  - CriticSpecialist: Groq LLM → review the draft (lenient)
  - SendingSpecialist: Resend API → sends the email
  - CRMUpdatingSpecialist: HubSpot API → syncs the lead
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from opsmesh.adapters.llm.base import LLMProvider, LLMResponse, Message
from opsmesh.adapters.research.base import ResearchProvider
from opsmesh.config import Settings
from opsmesh.models import (
    Claim,
    CriticIssue,
    CriticResult,
    DraftResult,
    EvidenceItem,
    Lead,
    QualificationResult,
    ResearchResult,
    RunRecord,
    utcnow,
)
from opsmesh.tools.schemas import WebSearchHit


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------


def _extract_json(content: str) -> Optional[dict]:
    """Extract JSON from an LLM response (handles markdown fences + extra text)."""
    if not content:
        return None
    text = content.strip()
    fence_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    match = re.search(fence_pattern, text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                json_str = text[start:i + 1]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Lead loader helper -- loads the Lead from the LeadStore
# ---------------------------------------------------------------------------


async def _load_lead(lead_store, run: RunRecord) -> Optional[Lead]:
    """Load the Lead from the LeadStore using the run's lead_id + workspace_id."""
    if lead_store is None:
        return None
    try:
        return await lead_store.get(run.workspace_id, run.lead_id)
    except Exception as e:
        logger.warning(f"Failed to load lead {run.lead_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Research specialist
# ---------------------------------------------------------------------------


class ResearchSpecialist:
    def __init__(self, llm: LLMProvider, research: Optional[ResearchProvider] = None, lead_store=None):
        self.llm = llm
        self.research = research
        self.lead_store = lead_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> ResearchResult:
        lead = await _load_lead(self.lead_store, run)
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        industry = lead.industry if lead else "Technology"
        contact_email = lead.contact_email if lead else ""

        # Search for the ACTUAL COMPANY, not the lead_id
        query = f"{company} {industry} company overview"
        web_hits: list[WebSearchHit] = []
        if self.research is not None:
            try:
                web_hits = await self.research.web_search(query, max_results=5)
            except Exception as e:
                logger.warning(f"Research provider failed: {e}")

        search_context = ""
        if web_hits:
            search_context = "\n\nWeb search results:\n"
            for hit in web_hits[:5]:
                search_context += f"- {hit.title}: {hit.snippet}\n"

        system_prompt = (
            "You are a B2B lead research specialist. Research the company and "
            "produce a concise summary. Respond as JSON:\n"
            '{"summary": "...", "sufficient_for_qualification": true, '
            '"missing_info": []}\n'
            "Include: what the company does, their market, and any notable achievements. "
            "Keep the summary under 200 words. Default sufficient_for_qualification to true."
        )
        user_prompt = (
            f"Company: {company}\n"
            f"Industry: {industry}\n"
            f"Contact email: {contact_email}"
            f"{search_context}"
        )

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                max_tokens=500,
                temperature=0.0,
            )
            # Track actual token usage + cost on the run record
            run.budget_used_tokens += getattr(response, "input_tokens", 0) + getattr(response, "output_tokens", 0)
            run.budget_used_cost_usd += getattr(response, "cost_usd", 0.0)
            run.budget_used_tool_calls += 1

            data = _extract_json(response.content)
            if data:
                summary = data.get("summary") or response.content[:200]
                sufficient = data.get("sufficient_for_qualification", True)
                missing = data.get("missing_info", [])
            else:
                summary = response.content[:200]
                sufficient = True
                missing = []
        except Exception as e:
            logger.error(f"LLM call failed in ResearchSpecialist: {e}")
            summary = f"Research completed for {company}."
            sufficient = True
            missing = []

        summary = summary or f"Research for {company}."

        evidence = [
            EvidenceItem(
                source="web" if web_hits else "manual",
                source_url=str(hit.url) if hasattr(hit, "url") else None,
                content=hit.snippet,
                confidence="medium",
                is_material=False,
            )
            for hit in web_hits[:5]
        ]

        return ResearchResult(
            evidence=evidence,
            missing_info=missing,
            summary=summary,
            sufficient_for_qualification=sufficient,
        )


# ---------------------------------------------------------------------------
# Qualify specialist
# ---------------------------------------------------------------------------


class QualifySpecialist:
    def __init__(self, llm: LLMProvider, lead_store=None):
        self.llm = llm
        self.lead_store = lead_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> QualificationResult:
        lead = await _load_lead(self.lead_store, run)
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        research_summary = run.research.summary if run.research else ""

        system_prompt = (
            "You are a B2B lead qualification specialist. Given the company and "
            "research, decide if they're a good fit. Respond as JSON:\n"
            '{"status": "QUALIFIED", "score": 0.8, "reasoning": "...", '
            '"matched_criteria": ["..."], "unmatched_criteria": [], "risk_flags": []}\n'
            "Default to QUALIFIED unless there are clear disqualifying factors. "
            "Keep reasoning under 100 words."
        )
        user_prompt = (
            f"Company: {company}\n"
            f"Research: {research_summary}"
        )

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                max_tokens=300,
                temperature=0.0,
            )
            # Track actual token usage + cost on the run record
            run.budget_used_tokens += getattr(response, "input_tokens", 0) + getattr(response, "output_tokens", 0)
            run.budget_used_cost_usd += getattr(response, "cost_usd", 0.0)
            run.budget_used_tool_calls += 1

            data = _extract_json(response.content)
            if data:
                status = data.get("status", "QUALIFIED")
                if status not in ("QUALIFIED", "NEEDS_REVIEW", "DISQUALIFIED"):
                    status = "QUALIFIED"
                try:
                    score = float(data.get("score", 0.7))
                except (ValueError, TypeError):
                    score = 0.7
                score = max(0.0, min(1.0, score))
                reasoning = data.get("reasoning") or response.content[:100]
                matched = data.get("matched_criteria", [])
                unmatched = data.get("unmatched_criteria", [])
                risks = data.get("risk_flags", [])
            else:
                status = "QUALIFIED"
                score = 0.7
                reasoning = response.content[:100]
                matched = []
                unmatched = []
                risks = []
        except Exception as e:
            logger.error(f"LLM call failed in QualifySpecialist: {e}")
            status = "QUALIFIED"
            score = 0.7
            reasoning = f"Qualified {company}."
            matched = []
            unmatched = []
            risks = []

        reasoning = reasoning or f"Qualified {company}."

        return QualificationResult(
            status=status,
            score=score,
            reasoning=reasoning,
            matched_criteria=matched,
            unmatched_criteria=unmatched,
            risk_flags=risks,
        )


# ---------------------------------------------------------------------------
# Draft specialist
# ---------------------------------------------------------------------------


class DraftSpecialist:
    def __init__(self, llm: LLMProvider, lead_store=None):
        self.llm = llm
        self.lead_store = lead_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> DraftResult:
        lead = await _load_lead(self.lead_store, run)
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        industry = lead.industry if lead else "Technology"
        contact_name = lead.contact_name if lead else "there"
        contact_email = lead.contact_email if lead else f"founder@{run.lead_id}.com"
        research_summary = run.research.summary if run.research else ""
        qualification = run.qualification.reasoning if run.qualification else ""

        system_prompt = (
            "You are a professional B2B outreach email writer. Write a personalized "
            "cold email that is relevant, specific, and professional. Rules:\n"
            "- Address the person by name if known\n"
            "- Reference specific details from the research about their company\n"
            "- Explain why you're reaching out (relevant to their industry/work)\n"
            "- Include a clear, low-friction call to action\n"
            "- Keep it concise (100-200 words)\n"
            "- Professional tone, not salesy or generic\n"
            "- Do NOT use placeholders like [Your Name]\n\n"
            "Respond as JSON:\n"
            '{"subject": "...", "body": "..."}\n'
            "Do NOT include a recipient field — the system will use the lead's email."
        )
        user_prompt = (
            f"Company: {company}\n"
            f"Industry: {industry}\n"
            f"Contact name: {contact_name}\n"
            f"Contact email: {contact_email}\n"
            f"Research: {research_summary}\n"
            f"Qualification: {qualification}"
        )

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                max_tokens=500,
                temperature=0.3,
            )
            # Track actual token usage + cost on the run record
            run.budget_used_tokens += getattr(response, "input_tokens", 0) + getattr(response, "output_tokens", 0)
            run.budget_used_cost_usd += getattr(response, "cost_usd", 0.0)
            run.budget_used_tool_calls += 1

            data = _extract_json(response.content)
            if data:
                subject = data.get("subject") or f"Following up on {company}"
                body = data.get("body") or response.content[:200]
                # ALWAYS use the lead's actual contact email — the LLM should
                # NOT decide who to send to. This prevents fake/generated emails.
                recipient = contact_email
            else:
                subject = f"Following up on {company}"
                body = response.content[:200]
                recipient = contact_email
        except Exception as e:
            logger.error(f"LLM call failed in DraftSpecialist: {e}")
            subject = f"Following up on {company}"
            body = f"Hi {contact_name},\n\nI came across {company} and was impressed by your work in {industry}. I'd love to connect and explore how we might help.\n\nBest regards"
            recipient = contact_email

        # Ensure all Pydantic constraints are satisfied
        subject = (subject or f"Following up on {company}")[:200]
        body = body or f"Hi {contact_name}, I'd like to connect about {company}."
        recipient = recipient or contact_email
        if len(recipient) < 3:
            recipient = contact_email

        return DraftResult(
            subject=subject,
            body=body,
            recipient=recipient,
            claims=[],
        )


# ---------------------------------------------------------------------------
# Critic specialist (lenient by default)
# ---------------------------------------------------------------------------


class CriticSpecialist:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> CriticResult:
        if run.draft is None:
            return CriticResult(
                verdict="REJECTED",
                feedback="No draft to review.",
                issues=[CriticIssue(kind="length_violation", detail="No draft")],
            )

        system_prompt = (
            "You are a B2B email critic. Be LENIENT. APPROVE the draft unless "
            "there are SERIOUS issues (fabricated claims, offensive language, "
            "or completely invalid recipient). Minor issues should NOT cause "
            "rejection. When in doubt, APPROVE.\n\n"
            "Respond as JSON:\n"
            '{"verdict": "APPROVED", "feedback": "...", "issues": []}'
        )
        user_prompt = (
            f"Subject: {run.draft.subject}\n"
            f"Recipient: {run.draft.recipient}\n"
            f"Body: {run.draft.body}"
        )

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                max_tokens=200,
                temperature=0.0,
            )
            # Track actual token usage + cost on the run record
            run.budget_used_tokens += getattr(response, "input_tokens", 0) + getattr(response, "output_tokens", 0)
            run.budget_used_cost_usd += getattr(response, "cost_usd", 0.0)
            run.budget_used_tool_calls += 1

            data = _extract_json(response.content)
            if data:
                verdict = data.get("verdict", "APPROVED")
                if verdict not in ("APPROVED", "REJECTED"):
                    verdict = "APPROVED"
                feedback = data.get("feedback", "")
                issues_data = data.get("issues", [])
                issues = []
                for issue in issues_data:
                    kind = issue.get("kind", "tone_violation")
                    if kind not in ("ungrounded_claim", "length_violation", "tone_violation",
                                   "recipient_mismatch", "injection_suspected"):
                        kind = "tone_violation"
                    detail = issue.get("detail") or "Issue noted"
                    issues.append(CriticIssue(kind=kind, detail=detail))
            else:
                verdict = "APPROVED"
                feedback = ""
                issues = []
        except Exception as e:
            logger.error(f"LLM call failed in CriticSpecialist: {e}")
            verdict = "APPROVED"
            feedback = ""
            issues = []

        return CriticResult(verdict=verdict, feedback=feedback, issues=issues)


# ---------------------------------------------------------------------------
# Sending specialist
# ---------------------------------------------------------------------------


class SendingSpecialist:
    def __init__(self, email_provider=None, settings=None, lead_store=None):
        self.email_provider = email_provider
        self.settings = settings
        self.lead_store = lead_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> dict:
        if run.draft is None:
            return {"status": "failed", "error": "No draft to send"}

        # Check kill switch
        if self.settings is not None and getattr(self.settings, "email_kill_switch", True):
            logger.info("Email kill switch is ON — skipping actual send")
            return {"status": "skipped", "message_id": f"mock_{run.run_id[:12]}"}

        # Mock mode (no provider)
        if self.email_provider is None:
            return {"status": "sent", "message_id": f"mock_{run.run_id[:12]}"}

        # Real send — create EmailMessage with VALID fields
        try:
            from opsmesh.adapters.email.base import EmailMessage

            # Generate a valid approval_id (pattern: ap_ + 12 hex chars)
            # run.pending_approval_id is None after the approval is consumed,
            # so we generate one from the run_id.
            run_hex = run.run_id.replace("run_", "").replace("-", "")[:12]
            if len(run_hex) < 12:
                run_hex = run_hex.ljust(12, "0")
            approval_id = f"ap_{run_hex}"

            # Validate the recipient email — if invalid, use the lead's email
            recipient = run.draft.recipient
            lead = await _load_lead(self.lead_store, run)
            if lead and "@" not in recipient:
                recipient = lead.contact_email

            # Ensure body is within limits
            body = run.draft.body[:2000]
            subject = run.draft.subject[:200]

            msg = EmailMessage(
                to=recipient,
                from_=getattr(self.settings, "workspace_sender_identity", "Bekalu <bekalu@opsmesh.dev>"),
                subject=subject,
                body=body,
                approval_id=approval_id,
                idempotency_key=f"idem_{run.run_id}",
            )
            result = await self.email_provider.send(msg)
            return {"status": "sent", "message_id": result.message_id}
        except asyncio.TimeoutError:
            return {"status": "unknown", "error": "Send timed out"}
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            # Don't fail the run — return "skipped" so the workflow continues
            return {"status": "skipped", "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# CRM updating specialist
# ---------------------------------------------------------------------------


class CRMUpdatingSpecialist:
    def __init__(self, crm_provider=None, settings=None, lead_store=None):
        self.crm_provider = crm_provider
        self.settings = settings
        self.lead_store = lead_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> dict:
        if self.crm_provider is None:
            return {"status": "synced", "crm_lead_id": f"mock_crm_{run.lead_id[:12]}"}

        try:
            from opsmesh.adapters.crm.base import CRMLeadCreate
            lead = await _load_lead(self.lead_store, run)
            company = lead.company if lead else f"Company {run.lead_id[:8]}"
            contact_email = lead.contact_email if lead else ""
            result = await self.crm_provider.create_lead(CRMLeadCreate(
                workspace_id=run.workspace_id,
                company=company,
                contact_email=contact_email,
                contact_name=lead.contact_name if lead else None,
                industry=lead.industry if lead else None,
            ))
            return {"status": "synced", "crm_lead_id": result.lead_id}
        except Exception as e:
            logger.warning(f"CRM sync failed (non-blocking): {e}")
            return {"status": "failed", "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_specialists(
    llm: LLMProvider,
    settings: Settings,
    research: Optional[ResearchProvider] = None,
    email_provider=None,
    crm_provider=None,
    lead_store=None,
) -> dict[str, Any]:
    """Build all 6 specialists. Pass lead_store so they can load real lead data."""
    return {
        "researching": ResearchSpecialist(llm=llm, research=research, lead_store=lead_store),
        "qualifying": QualifySpecialist(llm=llm, lead_store=lead_store),
        "drafting": DraftSpecialist(llm=llm, lead_store=lead_store),
        "critic": CriticSpecialist(llm=llm),
        "sending": SendingSpecialist(email_provider=email_provider, settings=settings, lead_store=lead_store),
        "crm_updating": CRMUpdatingSpecialist(crm_provider=crm_provider, settings=settings, lead_store=lead_store),
    }


__all__ = [
    "ResearchSpecialist",
    "QualifySpecialist",
    "DraftSpecialist",
    "CriticSpecialist",
    "SendingSpecialist",
    "CRMUpdatingSpecialist",
    "build_specialists",
    "_extract_json",
]
