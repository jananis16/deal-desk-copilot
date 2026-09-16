"""Narrative layer.

The evaluation decides. This module explains. Keeping those separate matters:
an approval decision has to be reproducible and auditable, while the write-up
for the approver is a language problem where a model genuinely helps.

If no API key is configured the module falls back to a deterministic template,
so the tool still runs end to end offline.
"""

from __future__ import annotations

import os
import textwrap

from evaluate import Evaluation

SYSTEM_PROMPT = """You are a deal desk analyst writing the internal summary that \
accompanies a deal going for approval.

Rules:
- Lead with the recommendation. An approver should know what you want in the first line.
- Be specific about numbers. Never restate a number without saying why it matters.
- Never invent facts that are not in the deal data.
- Escalate with a recommendation, not an open question.
- Write for a busy VP. Six sentences maximum. No preamble, no sign-off.
"""


def _fmt_money(x: float) -> str:
    return f"${x:,.0f}"


def _fallback(ev: Evaluation) -> str:
    """Deterministic summary. Used when no model is configured."""
    verdict = {
        "auto_approve": "Auto-approved. No action needed.",
        "approval_required": f"Recommend approval. Routes to {ev.tier_label}.",
        "blocked": "Cannot proceed on standard paper.",
    }[ev.decision]

    lines = [
        verdict,
        f"{ev.account_name}: {ev.deal_type.replace('_', ' ')}, {ev.term_months} months, "
        f"{_fmt_money(ev.net_tcv)} TCV ({_fmt_money(ev.acv)} ACV) at a "
        f"{ev.blended_discount_pct:.1f}% blended discount.",
    ]

    blocking = [e for e in ev.exceptions if e.severity == "blocking"]
    review = [e for e in ev.exceptions if e.severity == "review"]
    advisory = [e for e in ev.exceptions if e.severity == "advisory"]

    if blocking:
        lines.append("Blocking: " + " ".join(e.message for e in blocking))
    if review:
        lines.append("Needs review: " + " ".join(e.message for e in review))
    if advisory:
        lines.append("Worth noting: " + " ".join(e.message for e in advisory))

    if ev.approvers:
        lines.append(
            "Approvers: "
            + ", ".join(a.replace("_", " ") for a in ev.approvers)
            + f". Target turnaround {ev.sla_hours}h."
        )

    return "\n\n".join(lines)


def _build_user_prompt(ev: Evaluation, deal: dict) -> str:
    exc_text = "\n".join(
        f"- [{e.severity}] {e.message}" for e in ev.exceptions
    ) or "- none"

    return textwrap.dedent(
        f"""
        Deal: {ev.account_name} ({ev.deal_id})
        Type: {ev.deal_type}
        Term: {ev.term_months} months
        List TCV: {_fmt_money(ev.list_tcv)}
        Net TCV: {_fmt_money(ev.net_tcv)}
        ACV: {_fmt_money(ev.acv)}
        Blended discount: {ev.blended_discount_pct:.1f}%
        Competitive context: {deal.get('competitor') or 'none noted'}
        Rep notes: {deal.get('notes') or 'none'}

        Policy outcome: {ev.decision}
        Approval tier: {ev.tier_label}
        Approvers required: {', '.join(ev.approvers) or 'none'}
        Target turnaround: {ev.sla_hours} hours

        Exceptions raised:
        {exc_text}

        Write the approval summary.
        """
    ).strip()


def summarize(ev: Evaluation, deal: dict) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback(ev)

    try:
        import anthropic
    except ImportError:
        return _fallback(ev)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=os.environ.get("DEAL_DESK_MODEL", "claude-sonnet-4-5"),
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(ev, deal)}],
        )
        return response.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 - never fail the run on a model error
        return _fallback(ev) + f"\n\n(Model summary unavailable: {exc})"
