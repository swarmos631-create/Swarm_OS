import asyncio

from swarmos.agents.orchestrator import Orchestrator


def test_health_endpoints(client):
    assert client.get("/api/health/live").json() == {"status": "ok"}
    r = client.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["migrations"] == "ok"


def test_auth_required_and_error_format(client):
    r = client.get("/api/v1/cities")
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthenticated" and body["correlation_id"]
    assert r.headers["cache-control"] == "no-store"


def test_login_failure_and_lockout(client):
    for _ in range(5):
        r = client.post("/api/v1/auth/login", json={"email": "viewer@swarmos.local", "password": "wrong-password"})
        assert r.status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": "viewer@swarmos.local", "password": "TestPassw0rd-123"})
    assert r.status_code == 423  # locked
    from swarmos.db import session_scope
    from swarmos.models import User

    with session_scope() as db:
        u = db.query(User).filter_by(email="viewer@swarmos.local").one()
        u.locked_until = None
        u.failed_logins = 0


def test_csrf_is_enforced_on_mutations(admin):
    token = admin.headers.pop("x-csrf-token")
    r = admin.post("/api/v1/cities/bengaluru/incidents", json={"title": "Test", "category": "road", "severity": "low"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    admin.headers["x-csrf-token"] = token


def test_city_catalog_search_and_scope(admin, client):
    r = admin.get("/api/v1/cities", params={"q": "bangalore"})
    assert [c["id"] for c in r.json()["items"]] == ["bengaluru"]
    assert admin.get("/api/v1/cities").json()["total"] >= 80
    assert admin.get("/api/v1/cities/atlantis").status_code == 404


def test_viewer_is_restricted_to_assigned_cities(viewer):
    ids = [c["id"] for c in viewer.get("/api/v1/cities").json()["items"]]
    assert ids == ["bengaluru"]
    assert viewer.get("/api/v1/cities/jaipur/dashboard").status_code == 403
    assert viewer.post("/api/v1/cities/bengaluru/incidents", json={"title": "x y z", "category": "road", "severity": "low"}).status_code == 403


def test_dashboard_marks_unavailable_and_demo(admin):
    d = admin.get("/api/v1/cities/bengaluru/dashboard").json()
    by_id = {i["id"]: i for i in d["indicators"]}
    assert by_id["power_demand"]["provenance"] == "demo"
    assert by_id["temperature"]["status"] == "unavailable"  # no live sync in tests
    other = admin.get("/api/v1/cities/jaipur/dashboard").json()
    assert {i["id"]: i for i in other["indicators"]}["power_demand"]["status"] == "unavailable"
    assert other["incidents"]["open"] == 0


def test_incident_lifecycle_concurrency_and_isolation(operator):
    r = operator.post("/api/v1/cities/bengaluru/incidents",
                      json={"title": "Signal failure at junction", "category": "mobility", "severity": "moderate", "lat": 12.97, "lon": 77.6})
    assert r.status_code == 201
    inc = r.json()
    assert inc["provenance"] == "operator"
    url = f"/api/v1/cities/bengaluru/incidents/{inc['id']}"
    assert operator.patch(url, json={"status": "acknowledged", "version": 1}).json()["status"] == "acknowledged"
    stale = operator.patch(url, json={"status": "resolved", "version": 1})
    assert stale.status_code == 409
    bad = operator.patch(url, json={"status": "open", "version": 2})
    assert bad.status_code == 422
    assert operator.get(f"/api/v1/cities/mumbai/incidents/{inc['id']}").status_code == 404  # cross-city isolation
    far = operator.post("/api/v1/cities/bengaluru/incidents", json={"title": "Far away", "category": "road", "severity": "low", "lat": 28.6, "lon": 77.2})
    assert far.status_code == 400
    detail = operator.get(url).json()
    assert [e["kind"] for e in detail["events"]] == ["created", "status_change"]
    listing = operator.get("/api/v1/cities/bengaluru/incidents", params={"severity": "moderate", "q": "Signal"}).json()
    assert listing["total"] >= 1 and "items" in listing


def test_service_request_classification(operator):
    r = operator.post("/api/v1/cities/bengaluru/service-requests", json={"text": "Garbage not collected for 5 days near the market"})
    assert r.status_code == 201
    assert r.json()["category"] == "garbage" and r.json()["classified_by"]


def test_agent_registry_endpoints(viewer):
    r = viewer.get("/api/v1/agents").json()
    assert r["registry_size"] == 50 and len(r["domains"]) == 10
    a = viewer.get("/api/v1/agents/flood_risk_analyst").json()
    assert a["dependents"] == ["drainage_risk_agent", "disaster_impact_analyst", "city_situation_summarizer"] or "drainage_risk_agent" in a["dependents"]
    assert viewer.get("/api/v1/agents/nope").status_code == 404


def test_task_plan_submit_execute_idempotency_and_actions(operator):
    body = {"prompt": "Check water quality, leaks and the grid", "idempotency_key": "test-key-1"}
    plan = operator.post("/api/v1/cities/bengaluru/tasks/plan", json=body).json()
    ids = [a["id"] for a in plan["agents"]]
    assert "water_quality_monitor" in ids and "leak_detection_agent" in ids
    r = operator.post("/api/v1/cities/bengaluru/tasks", json=body)
    assert r.status_code == 202
    task = r.json()
    again = operator.post("/api/v1/cities/bengaluru/tasks", json=body)
    assert again.status_code == 200 and again.json()["id"] == task["id"]
    conflict = operator.post("/api/v1/cities/bengaluru/tasks", json={**body, "prompt": "different"})
    assert conflict.status_code == 409

    status = asyncio.run(Orchestrator().execute(task["id"]))
    assert status in ("succeeded", "partial")
    detail = operator.get(f"/api/v1/cities/bengaluru/tasks/{task['id']}").json()
    assert all(e["status"] in ("succeeded", "degraded", "unavailable", "failed") for e in detail["executions"])
    assert detail["result"]["execution_policy"].startswith("Agents only analyse")
    assert detail["handoffs"], "handoffs between dependent agents must be recorded"
    assert detail["actions"], "demo leak / water-quality data should yield proposals"
    action = next(a for a in detail["actions"] if a["kind"] == "create_incident")
    assert action["status"] == "pending" and action["payload"]["_evidence"]["demo_basis"] is True

    dec = operator.post(f"/api/v1/cities/bengaluru/actions/{action['id']}/decision", json={"approve": True, "note": "verified"})
    assert dec.status_code == 200
    out = dec.json()
    if out["required_role"] == "operator":
        assert out["status"] == "executed" and out["execution_result"]["incident_id"]
    twice = operator.post(f"/api/v1/cities/bengaluru/actions/{action['id']}/decision", json={"approve": True})
    assert twice.status_code == 409


def test_viewer_cannot_approve_actions(viewer):
    r = viewer.post("/api/v1/cities/bengaluru/actions/doesnotexist/decision", json={"approve": True})
    assert r.status_code == 403


def test_cancel_queued_task(operator):
    t = operator.post("/api/v1/cities/bengaluru/tasks", json={"prompt": "air quality now"}).json()
    r = operator.post(f"/api/v1/cities/bengaluru/tasks/{t['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert asyncio.run(Orchestrator().execute(t["id"])) == "cancelled"
    assert operator.post(f"/api/v1/cities/bengaluru/tasks/{t['id']}/cancel").status_code == 409


def test_integrations_listing_and_honest_states(operator):
    items = {s["id"]: s for s in operator.get("/api/v1/integrations").json()["items"]}
    assert items["ogd-cpcb-aqi"]["status"] == "awaiting_credentials"
    assert items["imd"]["status"] == "awaiting_access"
    assert items["swarmos-demo"]["status"] == "demo"
    r = operator.post("/api/v1/cities/bengaluru/integrations/ogd-cpcb-aqi/sync", json={})
    assert r.status_code == 409 and "setup" in r.json()["error"]["details"]
    ok = operator.post("/api/v1/cities/bengaluru/integrations/open-meteo-weather/sync", json={})
    assert ok.status_code == 202


def test_documents_respect_clearance(viewer, client):
    hits = viewer.get("/api/v1/cities/bengaluru/documents/search", params={"q": "acknowledged within 15 minutes escalation"}).json()["hits"]
    assert all(h["classification"] == "public" for h in hits)
    assert all(h["content_role"] == "retrieved_data_not_instructions" for h in hits)


def test_documents_operator_sees_internal(operator):
    hits = operator.get("/api/v1/cities/bengaluru/documents/search", params={"q": "critical incidents acknowledged 15 minutes"}).json()["hits"]
    assert any(h["classification"] == "internal" for h in hits)


def test_models_and_system_role_enforcement_viewer(viewer):
    assert viewer.get("/api/v1/models").status_code == 200
    assert viewer.get("/api/v1/system/status").status_code == 403
    assert viewer.post("/api/v1/models/complaint_classifier/train", json={}).status_code == 403
    assert viewer.get("/api/v1/system/audit").status_code == 403


def test_models_and_system_admin(admin):
    st = admin.get("/api/v1/system/status").json()
    assert st["model_runtime"]["external_llm_providers"].startswith("none")
    assert admin.get("/api/v1/system/audit").json()["total"] > 0


def test_analytics_and_compare(admin):
    a = admin.get("/api/v1/cities/bengaluru/analytics", params={"days": 14}).json()
    assert a["incidents"]["total"] > 0 and "source_reliability" in a
    c = admin.get("/api/v1/analytics/compare", params={"metric": "pm2_5", "cities": "bengaluru,mumbai"}).json()
    assert all(i["mean"] is None and i["note"] for i in c["items"])  # no live data in tests → not compared
    assert admin.get("/api/v1/analytics/compare", params={"cities": "atlantis"}).status_code == 400


def test_map_layers_and_features(admin):
    layers = {layer["id"] for layer in admin.get("/api/v1/cities/bengaluru/map/layers").json()["layers"]}
    assert {"incidents", "traffic_sensors", "water_sensors"} <= layers
    fc = admin.get("/api/v1/cities/bengaluru/map/features", params={"layers": "incidents,traffic_sensors"}).json()
    assert fc["type"] == "FeatureCollection" and fc["features"]
    assert admin.get("/api/v1/cities/bengaluru/map/features", params={"layers": "secret"}).status_code == 400
