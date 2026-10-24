"""Civic services skills."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from swarmos.agents.contracts import ActionDraft, AgentContext, AgentResult
from swarmos.agents.skills.common import age_hours, finding, ok, records_ref, unavailable
from swarmos.ai.runtime import ModelUnavailable
from swarmos.ml.complaints import CATEGORIES, rule_classify

DEPARTMENTS = {
    "roads": "Roads & Infrastructure",
    "streetlight": "Electrical (Street Lighting)",
    "garbage": "Solid Waste Management",
    "water_supply": "Water Supply",
    "drainage": "Storm Water Drains",
    "water_logging": "Storm Water Drains",
    "sewerage": "Sewerage",
    "trees": "Horticulture",
    "stray_animals": "Veterinary Services",
    "encroachment": "Town Planning / Enforcement",
    "noise": "Pollution Control Cell",
    "construction": "Building & Town Planning",
}
SLA_HOURS = {"roads": 168, "streetlight": 72, "garbage": 24, "water_supply": 48, "drainage": 72, "water_logging": 24,
             "sewerage": 48, "trees": 72, "stray_animals": 72, "encroachment": 336, "noise": 48, "construction": 168}


def classify_texts(ctx: AgentContext, texts: list[str]) -> tuple[list[tuple[str, float]], str, bool]:
    try:
        model = ctx.models.load("complaint_classifier")
        probs = model.estimator.predict_proba(texts)
        classes = list(model.estimator.classes_)
        out = [(classes[int(row.argmax())], float(row.max())) for row in probs]
        return out, model.label, False
    except ModelUnavailable:
        return [rule_classify(t) for t in texts], "rules:keyword_classifier", True


def complaint_classification(ctx: AgentContext) -> AgentResult:
    text = getattr(ctx.params, "text", None)
    reqs = ctx.data.service_requests(days=30)
    texts = [r.text for r in reqs]
    if text:
        texts = [text, *texts]
    if not texts:
        return unavailable(["citizen complaints / service requests"], "No service requests to classify.")
    labels, method, degraded = classify_texts(ctx, texts)
    dist = Counter(lbl for lbl, _ in labels)
    low_conf = sum(1 for _, p in labels if p < 0.5)
    metrics: dict = {"classified": len(labels), "distribution": dict(dist.most_common()), "low_confidence": low_conf,
                     "categories": list(CATEGORIES)}
    if text:
        metrics["input_classification"] = {"text": text[:200], "category": labels[0][0], "confidence": round(labels[0][1], 2),
                                           "department": DEPARTMENTS.get(labels[0][0])}
    # agreement between stored category and this classifier (only for already-labelled requests)
    stored = [(r.category, lbl) for r, (lbl, _) in zip(reqs, labels[1:] if text else labels, strict=False) if r.category]
    if stored:
        metrics["agreement_with_stored"] = round(sum(a == b for a, b in stored) / len(stored), 3)
    return ok(f"Classified {len(labels)} complaints; top category '{dist.most_common(1)[0][0]}'.",
              confidence=0.5 if degraded else 0.75, method=method, degraded=degraded, metrics=metrics,
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs} or {"operator"})],
              provenance={r.provenance for r in reqs} or {"operator"},
              limitations=(["Complaint classifier not promoted; keyword rules used."] if degraded else [])
              + ["Classifier is trained on SYNTHETIC complaint text; validate on real labelled complaints before relying on it."])


def service_routing(ctx: AgentContext) -> AgentResult:
    cls = ctx.up("complaint_classification_agent")
    reqs = ctx.data.service_requests(days=30)
    if not reqs and cls is None:
        return unavailable(["service requests"], "No service requests to route.")
    by_dept: Counter = Counter()
    unrouted = 0
    for r in reqs:
        dept = r.department or DEPARTMENTS.get(r.category or "")
        if dept:
            by_dept[dept] += 1
        else:
            unrouted += 1
    routing = cls.metrics.get("input_classification") if cls else None
    return ok(f"{len(reqs)} requests across {len(by_dept)} departments; {unrouted} unrouted.", confidence=0.75,
              method="rules:category_department_matrix", metrics={"by_department": dict(by_dept.most_common()), "unrouted": unrouted,
                                                                   "input_routing": routing, "matrix": DEPARTMENTS},
              provenance={r.provenance for r in reqs} | (set(cls.provenance) if cls else set()),
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs} or {"operator"})])


def resolution_tracker(ctx: AgentContext) -> AgentResult:
    reqs = ctx.data.service_requests(days=60)
    if not reqs:
        return unavailable(["service request history"], "No service requests in the last 60 days.")
    per: dict[str, dict] = defaultdict(lambda: {"total": 0, "resolved": 0, "within_sla": 0, "open_breached": 0})
    for r in reqs:
        cat = r.category or "uncategorised"
        sla = SLA_HOURS.get(cat, 168)
        row = per[cat]
        row["total"] += 1
        if r.resolved_at:
            row["resolved"] += 1
            if age_hours(r.resolved_at, r.created_at) <= sla:
                row["within_sla"] += 1
        elif age_hours(ctx.now, r.created_at) > sla:
            row["open_breached"] += 1
    for row in per.values():
        row["sla_compliance"] = round(row["within_sla"] / row["resolved"], 2) if row["resolved"] else None
    breached = sum(r["open_breached"] for r in per.values())
    findings = [finding(f"{cat}: {row['open_breached']} open requests past SLA", "", "moderate")
                for cat, row in sorted(per.items(), key=lambda kv: -kv[1]["open_breached"]) if row["open_breached"] >= 3][:5]
    return ok(f"{breached} open requests have breached SLA across {len(per)} categories.", confidence=0.8,
              method="rules:sla_matrix", findings=findings, metrics={"by_category": dict(per), "open_breached": breached, "sla_hours": SLA_HOURS},
              provenance={r.provenance for r in reqs},
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs})],
              limitations=["SLA targets are configurable defaults; align with the city's citizen charter."])


def asset_maintenance(ctx: AgentContext) -> AgentResult:
    assets = ctx.data.features(["asset_bridge", "asset_road", "asset_building"])
    reqs = ctx.data.service_requests(days=30, categories=("roads", "streetlight", "trees"))
    if not assets and not reqs:
        return unavailable(["public asset register / condition surveys"], f"No asset register is connected for {ctx.city.name}.")
    backlog = sorted((a for a in assets if (a.properties.get("condition_index") or 100) < 40),
                     key=lambda a: a.properties.get("condition_index") or 100)
    actions = [ActionDraft(kind="manual", title=f"Schedule structural inspection: {a.name}",
                           rationale=f"Condition index {a.properties.get('condition_index')} (last inspected {a.properties.get('last_inspected', 'unknown')}).",
                           payload={"asset": a.name, "layer": a.layer, "lat": a.lat, "lon": a.lon, "department": "Engineering"},
                           risk="moderate" if (a.properties.get("condition_index") or 100) < 25 else "low") for a in backlog[:5]]
    return ok(f"{len(backlog)} assets below condition threshold; {len(reqs)} related complaints in 30 days.", confidence=0.6,
              method="rules:condition_threshold", actions=actions,
              findings=[finding(f"Poor condition: {a.name}", f"Condition index {a.properties.get('condition_index')}", "moderate", a.lat, a.lon) for a in backlog[:5]],
              metrics={"assets": len(assets), "backlog": [{"name": a.name, "layer": a.layer, "condition_index": a.properties.get("condition_index")} for a in backlog[:20]],
                       "related_complaints_30d": len(reqs)},
              provenance={a.provenance for a in assets} | {r.provenance for r in reqs})


def service_quality(ctx: AgentContext) -> AgentResult:
    reqs = ctx.data.service_requests(days=28)
    resolved = [r for r in reqs if r.resolved_at]
    if len(resolved) < 5:
        return unavailable(["≥5 resolved service requests in 28 days"], "Too few resolved requests to analyse service quality.")
    recent: list[float] = []
    prior: list[float] = []
    for r in resolved:
        hrs = age_hours(r.resolved_at, r.created_at)  # type: ignore[arg-type]
        (recent if age_hours(ctx.now, r.created_at) <= 14 * 24 else prior).append(hrs)
    med_recent = statistics.median(recent) if recent else None
    med_prior = statistics.median(prior) if prior else None
    trend = None
    if med_recent and med_prior:
        trend = round((med_recent - med_prior) / med_prior, 2)
    findings = []
    if trend is not None and trend > 0.2:
        findings.append(finding("Resolution times worsening", f"Median up {trend:.0%} vs prior 14 days.", "moderate"))
    return ok(f"Median resolution {med_recent or 0:.0f} h (last 14 d) vs {med_prior or 0:.0f} h (prior 14 d).", confidence=0.7,
              method="statistical:median_comparison", findings=findings, provenance={r.provenance for r in reqs},
              metrics={"median_resolution_h_recent": med_recent, "median_resolution_h_prior": med_prior, "trend": trend,
                       "resolved": len(resolved)},
              extra_refs=[records_ref("service_requests", len(reqs), {r.provenance for r in reqs})])
