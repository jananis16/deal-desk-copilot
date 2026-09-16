"""Deterministic deal evaluation.

Everything in this module is reproducible and auditable. No model calls.
Given the same deal and the same config, it returns the same decision every
time, which is the only acceptable property for approval logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

SEVERITY_ORDER = {"advisory": 0, "review": 1, "blocking": 2}


def load_config() -> tuple[dict, dict]:
    with open(CONFIG_DIR / "approval_matrix.yaml") as f:
        matrix = yaml.safe_load(f)
    with open(CONFIG_DIR / "pricing_policy.yaml") as f:
        pricing = yaml.safe_load(f)
    return matrix, pricing


@dataclass
class Exception_:
    """A deviation from policy. Named with a trailing underscore to avoid
    shadowing the builtin."""

    code: str
    severity: str  # advisory | review | blocking
    message: str
    reviewers: list[str] = field(default_factory=list)


@dataclass
class Evaluation:
    deal_id: str
    account_name: str
    deal_type: str
    term_months: int
    list_tcv: float
    net_tcv: float
    acv: float
    blended_discount_pct: float
    tier: str
    tier_label: str
    approvers: list[str]
    sla_hours: int
    decision: str  # auto_approve | approval_required | blocked
    exceptions: list[Exception_]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exceptions"] = [asdict(e) for e in self.exceptions]
        return d


def _round(x: float) -> float:
    return float(round(x, 2))


def _line_totals(deal: dict, pricing: dict) -> tuple[float, float, list[dict]]:
    """Return (list_tcv, net_tcv, annotated_lines)."""
    catalog = pricing["products"]
    term_years = deal["term_months"] / 12.0

    list_tcv = 0.0
    net_tcv = 0.0
    lines = []

    for line in deal["lines"]:
        sku = line["sku"]
        product = catalog.get(sku)
        if product is None:
            raise ValueError(f"Unknown SKU: {sku}")

        qty = line.get("quantity", 1)
        discount = line.get("discount_pct", 0) / 100.0
        model = product["model"]

        if model == "one_time":
            gross = product["list_price"] * qty
        elif model == "usage":
            # Usage lines are priced off the committed minimum, not a guess at
            # consumption. Uncommitted usage is not contracted revenue.
            committed_units = line.get("committed_units", 0)
            gross = product["list_price_unit"] * committed_units * term_years
        else:  # seat or platform_fee, priced annually
            gross = product["list_price_annual"] * qty * term_years

        net = gross * (1 - discount)
        list_tcv += gross
        net_tcv += net

        lines.append(
            {
                "sku": sku,
                "name": product["name"],
                "model": model,
                "quantity": qty,
                "discount_pct": line.get("discount_pct", 0),
                "list_amount": _round(gross),
                "net_amount": _round(net),
                "max_discount_pct": product["max_discount_pct"],
            }
        )

    return _round(list_tcv), _round(net_tcv), lines


def _check_product_discounts(lines: list[dict]) -> list[Exception_]:
    out = []
    for line in lines:
        if line["discount_pct"] > line["max_discount_pct"]:
            out.append(
                Exception_(
                    code="product_discount_exceeded",
                    severity="review",
                    message=(
                        f"{line['name']} discounted {line['discount_pct']:.0f}% "
                        f"against a {line['max_discount_pct']}% product ceiling."
                    ),
                    reviewers=["deal_desk", "finance_director"],
                )
            )
    return out


def _check_hard_limits(
    deal: dict, blended_discount_pct: float, matrix: dict
) -> list[Exception_]:
    limits = matrix["policy_limits"]
    out = []

    if blended_discount_pct > limits["absolute_discount_floor_pct"]:
        out.append(
            Exception_(
                code="below_discount_floor",
                severity="blocking",
                message=(
                    f"Blended discount {blended_discount_pct:.1f}% breaches the "
                    f"{limits['absolute_discount_floor_pct']}% floor. This is not a "
                    "pricing decision, it is a margin decision."
                ),
                reviewers=["cro", "cfo"],
            )
        )

    if deal["term_months"] > limits["max_term_months"]:
        out.append(
            Exception_(
                code="term_exceeds_max",
                severity="blocking",
                message=(
                    f"Term of {deal['term_months']} months exceeds the "
                    f"{limits['max_term_months']} month maximum."
                ),
                reviewers=["cfo", "legal"],
            )
        )

    terms = deal.get("payment_terms", limits["standard_payment_terms"])
    if terms in limits["payment_terms_requiring_finance"]:
        out.append(
            Exception_(
                code="extended_payment_terms",
                severity="review",
                message=(
                    f"Payment terms {terms.replace('_', ' ')} are a working capital "
                    "decision. Finance approval required."
                ),
                reviewers=["finance_director"],
            )
        )
    elif (
        terms != limits["standard_payment_terms"]
        and terms not in limits["extended_payment_terms_allowed"]
        and terms not in limits["payment_terms_requiring_finance"]
    ):
        out.append(
            Exception_(
                code="nonstandard_payment_terms",
                severity="review",
                message=f"Payment terms {terms} are outside the approved set.",
                reviewers=["finance_director"],
            )
        )

    return out


def _check_usage_commitments(deal: dict, pricing: dict, matrix: dict) -> list[Exception_]:
    catalog = pricing["products"]
    min_commit = matrix["policy_limits"]["min_committed_minimum_for_usage"]
    out = []

    usage_lines = [
        line
        for line in deal["lines"]
        if catalog.get(line["sku"], {}).get("requires_committed_minimum")
    ]
    if not usage_lines:
        return out

    committed_value = deal.get("committed_minimum", 0)
    if committed_value < min_commit:
        out.append(
            Exception_(
                code="insufficient_usage_commitment",
                severity="review",
                message=(
                    f"Usage products present with a committed minimum of "
                    f"${committed_value:,.0f}, below the ${min_commit:,.0f} threshold. "
                    "Uncommitted usage does not forecast."
                ),
                reviewers=["deal_desk", "finance_director"],
            )
        )
    return out


def _check_ramp(deal: dict, matrix: dict) -> list[Exception_]:
    ramp = deal.get("ramp")
    if not ramp:
        return []

    rules = matrix["ramp_rules"]
    out = []

    if len(ramp) > rules["max_ramp_periods"]:
        out.append(
            Exception_(
                code="ramp_too_long",
                severity="review",
                message=(
                    f"{len(ramp)} ramp periods exceeds the "
                    f"{rules['max_ramp_periods']} period maximum."
                ),
                reviewers=["deal_desk", "finance_director"],
            )
        )

    steady_state = max(p["annual_amount"] for p in ramp)
    first = ramp[0]["annual_amount"]
    pct = (first / steady_state * 100) if steady_state else 0

    if pct < rules["min_first_period_pct_of_steady_state"]:
        out.append(
            Exception_(
                code="ramp_front_loaded_too_low",
                severity="review",
                message=(
                    f"Year one is {pct:.0f}% of steady state, below the "
                    f"{rules['min_first_period_pct_of_steady_state']}% minimum. "
                    "A ramp this steep is a discount in disguise."
                ),
                reviewers=["deal_desk", "finance_director"],
            )
        )
    return out


def _check_non_standard_terms(deal: dict, matrix: dict) -> list[Exception_]:
    catalog = matrix["non_standard_terms"]
    out = []
    for term in deal.get("non_standard_terms", []):
        spec = catalog.get(term)
        if spec is None:
            out.append(
                Exception_(
                    code="unknown_non_standard_term",
                    severity="review",
                    message=f"Term '{term}' is not in the policy catalog. Route to Legal.",
                    reviewers=["legal"],
                )
            )
            continue
        out.append(
            Exception_(
                code=term,
                severity="blocking" if spec["blocking"] else "review",
                message=f"{term.replace('_', ' ').capitalize()}: {spec['rationale']}",
                reviewers=spec["reviewers"],
            )
        )
    return out


def _check_advisories(
    deal: dict, lines: list[dict], net_tcv: float, blended: float, pricing: dict
) -> list[Exception_]:
    flags = pricing["advisory_flags"]
    policy = pricing["deal_type_policy"].get(deal["deal_type"], {})
    out = []

    target = policy.get("target_discount_pct")
    if target is not None and blended > target + flags["discount_above_target_pct_delta"]:
        out.append(
            Exception_(
                code="discount_above_deal_type_target",
                severity="advisory",
                message=(
                    f"Blended discount {blended:.1f}% is well above the "
                    f"{target}% target for {deal['deal_type'].replace('_', ' ')}. "
                    + policy.get("notes", "")
                ).strip(),
            )
        )

    services = sum(l["net_amount"] for l in lines if l["model"] == "one_time")
    if net_tcv and (services / net_tcv * 100) > flags["services_ratio_warning_pct"]:
        out.append(
            Exception_(
                code="services_heavy",
                severity="advisory",
                message=(
                    f"Services are {services / net_tcv * 100:.0f}% of TCV. "
                    "Delivery capacity is the risk here, not price."
                ),
            )
        )

    if deal["deal_type"] == "renewal" and deal.get("uplift_pct", 0) <= 0:
        out.append(
            Exception_(
                code="flat_renewal",
                severity="advisory",
                message=(
                    "Renewal carries no uplift. Flat is a real-terms decrease. "
                    "Worth a reason in the notes."
                ),
            )
        )

    return out


def _select_tier(
    blended_discount_pct: float, net_tcv: float, term_months: int, matrix: dict
) -> dict:
    for tier in matrix["approval_tiers"]:
        if blended_discount_pct > tier["max_discount_pct"]:
            continue
        if tier["max_tcv"] is not None and net_tcv > tier["max_tcv"]:
            continue
        if term_months > tier["max_term_months"]:
            continue
        return tier
    return matrix["approval_tiers"][-1]


def evaluate(deal: dict, matrix: dict | None = None, pricing: dict | None = None) -> Evaluation:
    if matrix is None or pricing is None:
        matrix, pricing = load_config()

    list_tcv, net_tcv, lines = _line_totals(deal, pricing)
    blended = ((list_tcv - net_tcv) / list_tcv * 100) if list_tcv else 0.0
    acv = net_tcv / (deal["term_months"] / 12.0) if deal["term_months"] else net_tcv

    exceptions: list[Exception_] = []
    exceptions += _check_product_discounts(lines)
    exceptions += _check_hard_limits(deal, blended, matrix)
    exceptions += _check_usage_commitments(deal, pricing, matrix)
    exceptions += _check_ramp(deal, matrix)
    exceptions += _check_non_standard_terms(deal, matrix)
    exceptions += _check_advisories(deal, lines, net_tcv, blended, pricing)

    exceptions.sort(key=lambda e: -SEVERITY_ORDER[e.severity])

    tier = _select_tier(blended, net_tcv, deal["term_months"], matrix)

    # Reviewers pulled in by exceptions are additive to the tier's approvers.
    approvers = list(tier["approvers"])
    for exc in exceptions:
        for r in exc.reviewers:
            if r not in approvers:
                approvers.append(r)

    has_blocking = any(e.severity == "blocking" for e in exceptions)
    needs_review = any(e.severity in ("blocking", "review") for e in exceptions)

    if has_blocking:
        decision = "blocked"
    elif not approvers and not needs_review:
        decision = "auto_approve"
    else:
        decision = "approval_required"

    return Evaluation(
        deal_id=deal["deal_id"],
        account_name=deal["account_name"],
        deal_type=deal["deal_type"],
        term_months=deal["term_months"],
        list_tcv=list_tcv,
        net_tcv=net_tcv,
        acv=_round(acv),
        blended_discount_pct=_round(blended),
        tier=tier["name"],
        tier_label=tier["label"],
        approvers=approvers,
        sla_hours=tier["sla_hours"],
        decision=decision,
        exceptions=exceptions,
    )
