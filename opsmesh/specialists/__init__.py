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
    def __init__(self, llm: LLMProvider, research: Optional[ResearchProvider] = None,
                 lead_store=None, history_store=None):
        self.llm = llm
        self.research = research
        self.lead_store = lead_store
        self.history_store = history_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> ResearchResult:
        lead = await _load_lead(self.lead_store, run)
        lead_type = getattr(lead, "lead_type", "company") if lead else "company"
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        industry = lead.industry if lead else "Technology"
        contact_email = lead.contact_email if lead else ""
        contact_name = lead.contact_name if lead else ""
        website = getattr(lead, "website", None) if lead else None
        notes = getattr(lead, "notes", None) if lead else None

        # --- Branch: company vs individual research ---
        if lead_type == "individual":
            result = await self._research_individual(
                run, contact_name, contact_email, website, notes, industry
            )
        else:
            result = await self._research_company(
                run, company, industry, contact_email
            )

        # Log a history event (the agent's memory)
        if self.history_store is not None:
            from opsmesh.models import LeadHistoryEvent
            target = company or contact_name or contact_email
            await self.history_store.save(LeadHistoryEvent(
                workspace_id=run.workspace_id,
                lead_id=run.lead_id,
                run_id=run.run_id,
                event_type="research",
                summary=f"Researched {lead_type} lead: {target}",
                detail=result.summary[:500] if result.summary else None,
            ))

        return result

    async def _research_company(self, run, company, industry, contact_email):
        """Research a company lead (original behavior)."""
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

    async def _research_individual(self, run, contact_name, contact_email,
                                    website, notes, industry):
        """Research an individual lead.

        If the person has a website, research it to understand their context.
        If not, use the notes/context they filled on the contact form.
        The goal is to understand the person's situation so the draft email
        can be personalized.
        """
        web_hits: list[WebSearchHit] = []
        if website and self.research is not None:
            # Research the person's website
            query = f"{website} {contact_name or ''} about"
            try:
                web_hits = await self.research.web_search(query, max_results=5)
            except Exception as e:
                logger.warning(f"Research provider failed for individual: {e}")

        search_context = ""
        if web_hits:
            search_context = "\n\nWeb search results:\n"
            for hit in web_hits[:5]:
                search_context += f"- {hit.title}: {hit.snippet}\n"

        system_prompt = (
            "You are a personal outreach research specialist. An individual "
            "has filled a contact form. Research their context (from their "
            "website if available, or from the details they provided) and "
            "produce a concise summary to personalize the outreach. Respond as JSON:\n"
            '{"summary": "...", "sufficient_for_qualification": true, '
            '"missing_info": []}\n'
            "Include: who they are, what they are looking for, and any relevant "
            "context from their website or notes. Keep the summary under 200 words. "
            "Default sufficient_for_qualification to true."
        )
        user_prompt = (
            f"Contact name: {contact_name or 'Unknown'}\n"
            f"Contact email: {contact_email}\n"
            f"Website: {website or 'None provided'}\n"
            f"Industry/interest: {industry or 'Unknown'}\n"
            f"Notes from contact form: {notes or 'None provided'}"
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
            logger.error(f"LLM call failed in ResearchSpecialist (individual): {e}")
            summary = f"Research completed for {contact_name or contact_email}."
            sufficient = True
            missing = []

        summary = summary or f"Research for {contact_name or contact_email}."

        evidence = [
            EvidenceItem(
                source="web" if web_hits else "form",
                source_url=str(hit.url) if hasattr(hit, "url") else (website if website else None),
                content=hit.snippet,
                confidence="medium",
                is_material=False,
            )
            for hit in web_hits[:5]
        ]
        # If no web hits but we have notes, add a lead-submitted evidence item
        if not evidence and notes:
            evidence = [
                EvidenceItem(
                    source="lead_submitted",
                    source_url=website,
                    content=notes[:500],
                    confidence="high",
                    is_material=True,
                )
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
    def __init__(self, llm: LLMProvider, lead_store=None, history_store=None):
        self.llm = llm
        self.lead_store = lead_store
        self.history_store = history_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> QualificationResult:
        lead = await _load_lead(self.lead_store, run)
        lead_type = getattr(lead, "lead_type", "company") if lead else "company"
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        contact_name = lead.contact_name if lead else ""
        notes = getattr(lead, "notes", None) if lead else None
        research_summary = run.research.summary if run.research else ""

        if lead_type == "individual":
            system_prompt = (
                "You are a lead qualification specialist. An individual filled a "
                "contact form. Given their context and research, decide if they are "
                "a good fit to follow up with. Respond as JSON:\n"
                '{"status": "QUALIFIED", "score": 0.8, "reasoning": "...", '
                '"matched_criteria": ["..."], "unmatched_criteria": [], "risk_flags": []}\n'
                "Default to QUALIFIED unless there are clear disqualifying factors. "
                "Keep reasoning under 100 words."
            )
            user_prompt = (
                f"Contact name: {contact_name or 'Unknown'}\n"
                f"Notes: {notes or 'None'}\n"
                f"Research: {research_summary}"
            )
            fallback_reasoning = f"Qualified individual {contact_name or lead.contact_email if lead else ''}."
        else:
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
            fallback_reasoning = f"Qualified {company}."

        try:
            response = await self.llm.chat(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                max_tokens=300,
                temperature=0.0,
            )
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
            reasoning = fallback_reasoning
            matched = []
            unmatched = []
            risks = []

        reasoning = reasoning or fallback_reasoning

        # Save the score (0-100) on the Lead so it shows in the leads list
        score_int = int(round(score * 100))
        if lead is not None and self.lead_store is not None:
            try:
                lead.score = score_int
                lead.updated_at = utcnow()
                await self.lead_store.save(lead)
            except Exception as e:
                logger.warning(f"Failed to save lead score: {e}")

        # Log a history event (the agent's memory)
        if self.history_store is not None:
            from opsmesh.models import LeadHistoryEvent
            target = company or contact_name or (lead.contact_email if lead else "")
            await self.history_store.save(LeadHistoryEvent(
                workspace_id=run.workspace_id,
                lead_id=run.lead_id,
                run_id=run.run_id,
                event_type="qualify",
                summary=f"Qualified {lead_type} lead: {target} (score {score_int}/100, {status})",
                detail=reasoning[:500],
            ))

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
    def __init__(self, llm: LLMProvider, lead_store=None, history_store=None):
        self.llm = llm
        self.lead_store = lead_store
        self.history_store = history_store

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> DraftResult:
        lead = await _load_lead(self.lead_store, run)
        lead_type = getattr(lead, "lead_type", "company") if lead else "company"
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        industry = lead.industry if lead else "Technology"
        contact_name = lead.contact_name if lead else "there"
        contact_email = lead.contact_email if lead else f"founder@{run.lead_id}.com"
        website = getattr(lead, "website", None) if lead else None
        notes = getattr(lead, "notes", None) if lead else None
        research_summary = run.research.summary if run.research else ""
        qualification = run.qualification.reasoning if run.qualification else ""

        # --- Branch: company vs individual draft ---
        if lead_type == "individual":
            system_prompt = (
                "You are a professional outreach email writer. An individual "
                "filled a contact form on a connected website. Write a warm, "
                "personalized reply that addresses their specific request. Rules:\n"
                "- Address the person by name if known\n"
                "- Reference what they asked about (from their notes/website)\n"
                "- Be helpful and specific, not generic or salesy\n"
                "- Include a clear next step (a question or an offer to help)\n"
                "- Keep it concise (100-200 words)\n"
                "- Professional, friendly tone\n"
                "- Do NOT use placeholders like [Your Name]\n\n"
                "Respond as JSON:\n"
                '{"subject": "...", "body": "..."}\n'
                "Do NOT include a recipient field -- the system will use the lead's email."
            )
            user_prompt = (
                f"Contact name: {contact_name}\n"
                f"Contact email: {contact_email}\n"
                f"Website: {website or 'None provided'}\n"
                f"Interest/industry: {industry or 'Unknown'}\n"
                f"What they asked about (notes): {notes or 'Not specified'}\n"
                f"Research: {research_summary}\n"
                f"Qualification: {qualification}"
            )
            fallback_subject = f"Reaching out, {contact_name}" if contact_name != "there" else "Thanks for reaching out"
            fallback_body = (
                f"Hi {contact_name},\n\n"
                f"Thanks for reaching out"
                + (f" via {website}" if website else "")
                + ". "
                + (f"I saw your question about {notes[:100]}." if notes else "I'd love to learn more about what you're looking for.")
                + " I'd be happy to help -- would you be open to a quick chat this week?\n\nBest regards"
            )
        else:
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
                "Do NOT include a recipient field -- the system will use the lead's email."
            )
            user_prompt = (
                f"Company: {company}\n"
                f"Industry: {industry}\n"
                f"Contact name: {contact_name}\n"
                f"Contact email: {contact_email}\n"
                f"Research: {research_summary}\n"
                f"Qualification: {qualification}"
            )
            fallback_subject = f"Following up on {company}"
            fallback_body = (
                f"Hi {contact_name},\n\nI came across {company} and was impressed "
                f"by your work in {industry}. I'd love to connect and explore how "
                f"we might help.\n\nBest regards"
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
            run.budget_used_tokens += getattr(response, "input_tokens", 0) + getattr(response, "output_tokens", 0)
            run.budget_used_cost_usd += getattr(response, "cost_usd", 0.0)
            run.budget_used_tool_calls += 1

            data = _extract_json(response.content)
            if data:
                subject = data.get("subject") or fallback_subject
                body = data.get("body") or response.content[:200]
                recipient = contact_email
            else:
                subject = fallback_subject
                body = response.content[:200]
                recipient = contact_email
        except Exception as e:
            logger.error(f"LLM call failed in DraftSpecialist: {e}")
            subject = fallback_subject
            body = fallback_body
            recipient = contact_email

        # Ensure all Pydantic constraints are satisfied
        subject = (subject or fallback_subject)[:200]
        body = body or fallback_body
        recipient = recipient or contact_email
        if len(recipient) < 3:
            recipient = contact_email

        draft_result = DraftResult(
            subject=subject,
            body=body,
            recipient=recipient,
            claims=[],
        )

        # Log a history event (the agent's memory)
        if self.history_store is not None:
            from opsmesh.models import LeadHistoryEvent
            await self.history_store.save(LeadHistoryEvent(
                workspace_id=run.workspace_id,
                lead_id=run.lead_id,
                run_id=run.run_id,
                event_type="draft",
                summary=f"Drafted email: {subject}",
                detail=body[:500],
            ))

        return draft_result


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
    def __init__(self, email_provider=None, settings=None, lead_store=None,
                 sent_email_store=None, history_store=None, gateway=None):
        self.email_provider = email_provider
        self.settings = settings
        self.lead_store = lead_store
        self.sent_email_store = sent_email_store
        self.history_store = history_store
        # Stage B Phase 2 (P0-1): the ToolGateway enforces the 7-check policy
        # layer (approval verify-and-consume, payload hash binding, scope,
        # rate limit, idempotency, audit) at the actual send site. When set,
        # real sends go through gateway.execute("send_email", ...) with the
        # REAL run.pending_approval_id (not the synthesized f"ap_{run_hex}").
        # When None (dev/test), the old direct-provider path is used.
        self.gateway = gateway

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> dict:
        if run.draft is None:
            return {"status": "failed", "error": "No draft to send"}

        # Resolve recipient + sender + content (needed for logging even on skip)
        recipient = run.draft.recipient
        lead = await _load_lead(self.lead_store, run)
        if lead and "@" not in recipient:
            recipient = lead.contact_email
        from opsmesh.config import get_settings
        from_address = get_settings().workspace_sender_identity
        body = run.draft.body[:2000]
        subject = run.draft.subject[:200]
        lead_type = getattr(lead, "lead_type", "company") if lead else "company"

        # Helper: log the send attempt to the SentEmailStore (durable record)
        async def _log(status, message_id=None):
            if self.sent_email_store is not None:
                from opsmesh.models import SentEmail
                await self.sent_email_store.save(SentEmail(
                    workspace_id=run.workspace_id,
                    lead_id=run.lead_id,
                    run_id=run.run_id,
                    recipient=recipient,
                    from_address=from_address,
                    subject=subject,
                    body=body,
                    status=status,
                    provider_message_id=message_id,
                    lead_type=lead_type,
                ))
            # Also log a history event (the agent's memory)
            if self.history_store is not None:
                from opsmesh.models import LeadHistoryEvent
                await self.history_store.save(LeadHistoryEvent(
                    workspace_id=run.workspace_id,
                    lead_id=run.lead_id,
                    run_id=run.run_id,
                    event_type="send",
                    summary=f"Email {status}: {subject} -> {recipient}",
                    detail=body[:500],
                ))

        # Check kill switch
        if self.settings is not None and getattr(self.settings, "email_kill_switch", True):
            logger.info("Email kill switch is ON -- skipping actual send")
            mock_id = f"mock_{run.run_id[:12]}"
            await _log("skipped", mock_id)
            return {"status": "skipped", "message_id": mock_id}

        # Stage B Phase 1 (P0-3): demo-mode recipient restriction.
        # In single_user_demo mode, real sends are restricted to the
        # configured OPSMESH_EMAIL_TEST_RECIPIENT (Resend's verified test
        # recipient). This prevents the demo from being an open email relay.
        from opsmesh.config import is_demo_mode
        if self.settings is not None and is_demo_mode(self.settings):
            test_recipient = getattr(self.settings, "email_test_recipient", "")
            if not test_recipient:
                logger.warning(
                    "Demo mode: email_test_recipient not configured — blocking send"
                )
                await _log("failed")
                return {
                    "status": "failed",
                    "error": "Demo mode requires OPSMESH_EMAIL_TEST_RECIPIENT to be set",
                }
            # Normalize for comparison (strip display-name + whitespace).
            def _bare(addr: str) -> str:
                if "<" in addr and ">" in addr:
                    return addr.split("<", 1)[1].split(">", 1)[0].strip().lower()
                return addr.strip().lower()
            if _bare(recipient) != _bare(test_recipient):
                logger.warning(
                    f"Demo mode: recipient {recipient!r} != test recipient "
                    f"{test_recipient!r} — blocking send"
                )
                await _log("failed")
                return {
                    "status": "failed",
                    "error": "Demo mode restricts sends to the configured test recipient",
                }

        # Mock mode (no provider) -- logs a synthetic send.
        if self.email_provider is None:
            mock_id = f"mock_{run.run_id[:12]}"
            await _log("sent", mock_id)
            return {"status": "sent", "message_id": mock_id}

        # Stage B Phase 2 (P0-1): when the ToolGateway is wired, route the
        # real send through gateway.execute("send_email", ...). The gateway
        # enforces the 7-check policy layer at the SEND SITE — most critically,
        # it calls RealApprovalVerifier.verify_and_consume with the REAL
        # run.pending_approval_id and the current payload hash. A modified
        # recipient/subject/body after approval is rejected (StaleApprovalError).
        # Fail closed: if there is no pending_approval_id, the send is blocked.
        if self.gateway is not None:
            return await self._send_via_gateway(run, recipient, from_address, subject, body, _log)

        # Dev/test path (no gateway): direct provider call. The synthesized
        # approval_id here is ONLY for the provider's audit trail — it does
        # NOT authorize anything (the gateway is what enforces approval).
        try:
            from opsmesh.adapters.email.base import EmailMessage

            run_hex = run.run_id.replace("run_", "").replace("-", "")[:12]
            if len(run_hex) < 12:
                run_hex = run_hex.ljust(12, "0")
            approval_id = run.pending_approval_id or f"ap_{run_hex}"

            msg = EmailMessage(
                to=recipient,
                from_=from_address,
                subject=subject,
                body=body,
                approval_id=approval_id,
                idempotency_key=f"idem_{run.run_id}",
            )
            result = await self.email_provider.send(msg)
            await _log("sent", result.message_id)
            return {"status": "sent", "message_id": result.message_id}
        except asyncio.TimeoutError:
            await _log("unknown")
            return {"status": "unknown", "error": "Send timed out"}
        except Exception as e:
            logger.error(f"Email send failed: {type(e).__name__}: {e}")
            await _log("failed")
            return {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}

    async def _send_via_gateway(self, run, recipient, from_address, subject, body, _log):
        """Route the email send through the ToolGateway (P0-1).

        The gateway verifies the Gate 2 approval (existence, expiration,
        consumption, workspace, run, action, payload_hash) BEFORE the
        provider is called. The approval_id is run.pending_approval_id — the
        REAL one created by the supervisor when the run entered
        AWAITING_APPROVAL, NOT the synthesized f"ap_{run_hex}".
        """
        from opsmesh.approval_store import compute_payload_hash, TOOL_PROTECTED_FIELDS
        from opsmesh.security_context import AuthContext
        from opsmesh.tool_gateway import GatewayError

        approval_id = run.pending_approval_id
        if not approval_id:
            logger.warning(f"Send blocked: run {run.run_id} has no pending_approval_id (Gate 2 not approved)")
            await _log("failed")
            return {
                "status": "failed",
                "error": "No Gate 2 approval (pending_approval_id is empty) — send blocked",
            }

        payload = {
            "to": recipient,
            "from_": from_address,
            "subject": subject,
            "body": body,
            # approval_id is NOT a protected field (not in TOOL_PROTECTED_FIELDS),
            # so it doesn't affect the payload_hash. It's passed through so the
            # provider_router can populate the EmailMessage's audit-trail field.
            "approval_id": approval_id,
        }
        protected = TOOL_PROTECTED_FIELDS.get("send_email")
        # Compute the hash from the PROTECTED fields only (to, from_, subject, body).
        protected_payload = {k: payload[k] for k in protected if k in payload}
        payload_hash = compute_payload_hash("send_email", protected_payload, protected)

        # The AuthContext carries the trusted workspace_id (from the run, never
        # from LLM args) + the scopes the SendingSpecialist needs.
        auth = AuthContext(
            workspace_id=run.workspace_id,
            scopes=["email.send", "crm.read", "crm.write", "notify.send", "web.search"],
        )

        try:
            result = await self.gateway.execute(
                "send_email",
                payload,
                auth=auth,
                run_id=run.run_id,
                step_id=run.step_id,
                approval_id=approval_id,
                payload_hash=payload_hash,
                actor="sending_specialist",
            )
            # The provider_router returns the SendResult; extract message_id.
            message_id = getattr(result, "message_id", None) or str(result)
            await _log("sent", message_id)
            return {"status": "sent", "message_id": message_id}
        except GatewayError as e:
            logger.warning(
                f"Send blocked by gateway: [{e.tool_name}] {e.code}: {e.message}"
            )
            await _log("failed")
            return {
                "status": "failed",
                "error": f"gateway:{e.code}:{e.message[:140]}",
            }
        except Exception as e:
            logger.error(f"Email send failed: {type(e).__name__}: {e}")
            await _log("failed")
            return {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ---------------------------------------------------------------------------
# CRM updating specialist
# ---------------------------------------------------------------------------


class CRMUpdatingSpecialist:
    def __init__(self, crm_provider=None, settings=None, lead_store=None,
                 outbox_store=None, gateway=None):
        self.crm_provider = crm_provider
        self.settings = settings
        self.lead_store = lead_store
        # Phase 3 wires the outbox_store so CRM writes go through the drainer
        # instead of the provider directly. Kept optional for back-compat.
        self.outbox_store = outbox_store
        # Stage B Phase 2 (P0-1): when set, CRM syncs route through the
        # ToolGateway (scope, rate limit, idempotency, audit) via the
        # sync_lead_to_crm tool. When None (dev/test), direct provider call.
        self.gateway = gateway

    async def __call__(self, run: RunRecord, budget: Any = None, ctx: Any = None) -> dict:
        # Stage B Phase 1 (P0-4): demo-mode CRM guard.
        # In single_user_demo mode, live HubSpot mutations are disabled by
        # default (crm_demo_mode == "read_only"). The run still transitions
        # to SENT — CRM sync is non-blocking. Phase 3 replaces this skip
        # with a proper CRMOutboxOp (status SKIPPED_DEMO) so the outbox
        # table records the decision.
        from opsmesh.config import is_demo_mode
        if self.settings is not None and is_demo_mode(self.settings):
            crm_demo_mode = getattr(self.settings, "crm_demo_mode", "read_only")
            if crm_demo_mode in ("read_only", "disabled"):
                logger.info(
                    f"CRM demo mode ({crm_demo_mode}) — skipping live HubSpot mutation"
                )
                return {
                    "status": "skipped_demo",
                    "crm_lead_id": None,
                    "reason": f"crm_demo_mode={crm_demo_mode}",
                }

        if self.crm_provider is None:
            return {"status": "synced", "crm_lead_id": f"mock_crm_{run.lead_id[:12]}"}

        # Load the lead data (needed for outbox + gateway + direct paths).
        lead = await _load_lead(self.lead_store, run)
        company = lead.company if lead else f"Company {run.lead_id[:8]}"
        contact_email = lead.contact_email if lead else ""
        contact_name = lead.contact_name if lead else None
        industry = lead.industry if lead else None

        # Stage B Phase 3: when the outbox_store is wired, ENQUEUE a CRM outbox
        # op instead of calling the CRM synchronously. The CRMOutboxDrainer
        # (background task) claims + drains it independently. This decouples
        # CRM sync from the email send (Correction 2: CRM failure never
        # triggers email resend). The run transitions to SENT regardless.
        if self.outbox_store is not None:
            from opsmesh.models import CRMOutboxOp, CRMOutboxOperation
            op = CRMOutboxOp(
                workspace_id=run.workspace_id,
                run_id=run.run_id,
                lead_id=run.lead_id,
                operation=CRMOutboxOperation.CREATE_LEAD,
                args={
                    "company": company,
                    "contact_email": contact_email,
                    "contact_name": contact_name or "",
                    "industry": industry or "",
                },
            )
            try:
                self.outbox_store.enqueue(op)
                logger.info(f"Enqueued CRM outbox op {op.id} for run {run.run_id}")
                return {"status": "enqueued", "op_id": op.id, "crm_lead_id": None}
            except Exception as e:
                logger.warning(f"Failed to enqueue CRM outbox op: {e}")
                # Fall through to the gateway/direct path as a fallback.
                # The send already happened; CRM sync is non-blocking.

        # Stage B Phase 2 (P0-1): route through the gateway when wired.
        if self.gateway is not None:
            return await self._sync_via_gateway(
                run, company, contact_email, contact_name, industry
            )

        # Dev/test path (no gateway, no outbox): direct provider call.
        try:
            from opsmesh.adapters.crm.base import CRMLeadCreate
            result = await self.crm_provider.create_lead(CRMLeadCreate(
                workspace_id=run.workspace_id,
                company=company,
                contact_email=contact_email,
                contact_name=contact_name,
                industry=industry,
            ))
            return {"status": "synced", "crm_lead_id": result.lead_id}
        except Exception as e:
            logger.warning(f"CRM sync failed (non-blocking): {e}")
            return {"status": "failed", "error": str(e)[:200]}

    async def _sync_via_gateway(self, run, company, contact_email, contact_name, industry):
        """Route the CRM sync through the ToolGateway (P0-1).

        Uses the sync_lead_to_crm tool (WRITE, no fresh approval required —
        the lead was already Gate-2-approved for outreach). The gateway still
        enforces scope, rate limit, idempotency, and audit.
        """
        from opsmesh.security_context import AuthContext
        from opsmesh.tool_gateway import GatewayError

        args = {
            "workspace_id": run.workspace_id,
            "company": company,
            "contact_email": contact_email,
            "contact_name": contact_name or "",
            "industry": industry or "",
            "lead_id": run.lead_id,
        }
        auth = AuthContext(
            workspace_id=run.workspace_id,
            scopes=["crm.read", "crm.write", "email.send", "notify.send", "web.search"],
        )
        try:
            result = await self.gateway.execute(
                "sync_lead_to_crm",
                args,
                auth=auth,
                run_id=run.run_id,
                step_id=run.step_id,
                approval_id=None,  # no fresh approval needed (Gate 2 already approved)
                payload_hash=None,
                actor="crm_updating_specialist",
            )
            crm_lead_id = getattr(result, "lead_id", None) or str(result)
            return {"status": "synced", "crm_lead_id": crm_lead_id}
        except GatewayError as e:
            logger.warning(
                f"CRM sync blocked by gateway: [{e.tool_name}] {e.code}: {e.message}"
            )
            # Non-blocking: the run still goes to SENT.
            return {"status": "failed", "error": f"gateway:{e.code}:{e.message[:140]}"}
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
    sent_email_store=None,
    history_store=None,
    outbox_store=None,
    gateway=None,
) -> dict[str, Any]:
    """Build all 6 specialists.

    Pass lead_store so they can load real lead data.
    Pass sent_email_store so the SendingSpecialist logs every send to the
    durable sent-email log (visible in the Activity tab).
    Pass history_store so each specialist logs a LeadHistoryEvent after
    it runs (the agent's memory -- visible in the Activity tab + lead detail).
    Pass outbox_store (Phase 3) so CRMUpdatingSpecialist writes a CRMOutboxOp
    instead of calling the CRM directly.
    Pass gateway (Phase 2) so SendingSpecialist + CRMUpdatingSpecialist route
    their side effects through the ToolGateway (approval verify-and-consume,
    payload hash binding, scope, rate limit, idempotency, audit).
    """
    return {
        "researching": ResearchSpecialist(llm=llm, research=research, lead_store=lead_store, history_store=history_store),
        "qualifying": QualifySpecialist(llm=llm, lead_store=lead_store, history_store=history_store),
        "drafting": DraftSpecialist(llm=llm, lead_store=lead_store, history_store=history_store),
        "critic": CriticSpecialist(llm=llm),
        "sending": SendingSpecialist(
            email_provider=email_provider,
            settings=settings,
            lead_store=lead_store,
            sent_email_store=sent_email_store,
            history_store=history_store,
            gateway=gateway,
        ),
        "crm_updating": CRMUpdatingSpecialist(
            crm_provider=crm_provider,
            settings=settings,
            lead_store=lead_store,
            outbox_store=outbox_store,
            gateway=gateway,
        ),
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
