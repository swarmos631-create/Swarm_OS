import asyncio

import pytest

from swarmos.agents.orchestrator import Orchestrator


def test_shared_database_rate_limit_counter_is_atomic_and_hashes_caller_key():
    from swarmos.core.errors import ApiError
    from swarmos.core.ratelimit import RateLimiter, limiter
    from swarmos.db import session_scope
    from swarmos.models import SharedRateLimitBucket

    limiter.reset()
    another_api_instance = RateLimiter()
    caller = "public-report:198.51.100.25"
    limiter.check(caller, 2, window_s=60)
    another_api_instance.check(caller, 2, window_s=60)
    with pytest.raises(ApiError) as exc:
        limiter.check(caller, 2, window_s=60)
    assert exc.value.status == 429
    with session_scope() as db:
        row = db.query(SharedRateLimitBucket).one()
        assert row.hits == 2
        assert "198.51.100.25" not in row.bucket_key


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
    # No live sync in tests: weather comes from the climate-calibrated simulation and is labelled as such.
    assert by_id["temperature"]["status"] == "available" and by_id["temperature"]["provenance"] == "demo"
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


def test_incident_evidence_is_scoped_audited_and_operator_managed(operator):
    incident_response = operator.post("/api/v1/cities/bengaluru/incidents", json={
        "title": "Drain overflow near market", "category": "drainage", "severity": "high",
    })
    assert incident_response.status_code == 201, incident_response.text
    incident = incident_response.json()
    request = operator.post("/api/v1/cities/bengaluru/service-requests", json={
        "text": "Water overflowing from the drain near the market since this morning",
    }).json()
    base = f"/api/v1/cities/bengaluru/incidents/{incident['id']}"
    candidates = operator.get(f"{base}/evidence/candidates", params={"source_type": "service_request"})
    assert candidates.status_code == 200
    assert request["id"] in {item["source_id"] for item in candidates.json()["items"]}

    link = operator.post(f"{base}/evidence", json={
        "source_type": "service_request", "source_id": request["id"],
        "relationship": "supports", "note": "Citizen report describes the same location and overflow.",
    })
    assert link.status_code == 201, link.text
    evidence = link.json()
    assert evidence["provenance"] == request["provenance"]
    assert "not independently verified" in evidence["limitations"][0]
    assert request["id"] not in {item["source_id"] for item in operator.get(
        f"{base}/evidence/candidates", params={"source_type": "service_request"}).json()["items"]}
    assert operator.post(f"{base}/evidence", json={
        "source_type": "service_request", "source_id": request["id"],
        "relationship": "supports", "note": "Duplicate link should be rejected.",
    }).status_code == 409

    from fastapi.testclient import TestClient

    from swarmos.core.ratelimit import limiter
    from swarmos.main import create_app

    limiter.reset()
    with TestClient(create_app()) as viewer:
        csrf = viewer.post("/api/v1/auth/login", json={"email": "viewer@swarmos.local", "password": "TestPassw0rd-123"})
        assert csrf.status_code == 200
        viewer.headers["x-csrf-token"] = csrf.json()["csrf_token"]
        assert viewer.get(f"{base}/evidence/candidates", params={"source_type": "service_request"}).status_code == 403
        detail = viewer.get(base)
        assert detail.status_code == 200
        assert detail.json()["evidence"][0]["id"] == evidence["id"]
        assert viewer.delete(f"{base}/evidence/{evidence['id']}").status_code == 403
    assert operator.get(f"/api/v1/cities/mumbai/incidents/{incident['id']}").status_code == 404
    other_city_request = operator.post("/api/v1/cities/mumbai/service-requests", json={
        "text": "Garbage collecting delayed near the central market",
    }).json()
    assert operator.post(f"{base}/evidence", json={
        "source_type": "service_request", "source_id": other_city_request["id"],
        "relationship": "context", "note": "Cross-city record must never be accepted.",
    }).status_code == 404
    removed = operator.delete(f"{base}/evidence/{evidence['id']}")
    assert removed.status_code == 204
    assert operator.get(base).json()["evidence"] == []


def test_saved_scenario_uses_existing_durable_task_runner(operator):
    response = operator.post("/api/v1/cities/bengaluru/scenarios", json={
        "name": "Heavy rainfall stress test", "extra_rainfall_mm": 125, "temperature_delta_c": 2.5,
    })
    assert response.status_code == 202, response.text
    scenario = response.json()
    assert scenario["status"] == "queued"
    assert scenario["params"] == {"extra_rainfall_mm": 125, "temperature_delta_c": 2.5}
    task = operator.get(f"/api/v1/cities/bengaluru/tasks/{scenario['task_id']}").json()
    assert task["params"]["scenario_simulation_agent"] == scenario["params"]
    task_agents = [agent["id"] for agent in task["plan"]["agents"]]
    assert "scenario_simulation_agent" in task_agents
    assert task_agents[-2:] == ["city_operations_planning_agent", "compliance_checklist_agent"]
    listed = operator.get("/api/v1/cities/bengaluru/scenarios").json()
    assert listed["total"] >= 1
    assert listed["items"][0]["id"] == scenario["id"]
    assert listed["items"][0]["task_id"] == scenario["task_id"]
    assert operator.get("/api/v1/cities/mumbai/scenarios").json()["total"] == 0
    assert operator.post("/api/v1/cities/bengaluru/scenarios", json={
        "name": "Too much", "extra_rainfall_mm": 501, "temperature_delta_c": 0,
    }).status_code == 422


def test_public_citizen_intake_returns_private_reference_and_operator_triage(operator):
    response = operator.post("/api/v1/public/cities/bengaluru/service-requests", json={
        "text": "The streetlight on 4th cross has not worked for several nights",
    })
    assert response.status_code == 201, response.text
    submitted = response.json()
    assert len(submitted["reference"]) == 32
    assert "text" not in submitted and "email" not in submitted
    status_url = f"/api/v1/public/cities/bengaluru/service-requests/{submitted['reference']}"
    assert operator.get(status_url).json()["status"] == "open"
    assert operator.get(f"/api/v1/public/cities/mumbai/service-requests/{submitted['reference']}").status_code == 404
    staff = operator.get("/api/v1/cities/bengaluru/service-requests").json()
    assert any(item["id"] == submitted["reference"] and item["provenance"] == "public" for item in staff["items"])
    assert operator.patch(f"/api/v1/cities/bengaluru/service-requests/{submitted['reference']}",
                          json={"status": "in_progress"}).json()["status"] == "in_progress"
    assert operator.patch(f"/api/v1/cities/bengaluru/service-requests/{submitted['reference']}",
                          json={"status": "resolved"}).json()["status"] == "resolved"
    assert operator.get(status_url).json()["status"] == "resolved"


def test_sakhi_route_returns_real_route_shape_and_caveats_without_storage(client, monkeypatch):
    from swarmos.db import session_scope
    from swarmos.models import GeoFeature
    from swarmos.services import routing

    with session_scope() as db:
        db.add(GeoFeature(city_id="bengaluru", source_id="osm-vector-tiles", layer="bus_station",
                          external_id="test/bus-station-sakhi-route", name="Mapped bus station",
                          lat=12.975, lon=77.595, properties={"wheelchair": "yes"}, provenance="live"))
        db.add(GeoFeature(city_id="bengaluru", source_id="osm-overpass", layer="street_light",
                          external_id="node/sakhi-route-lamp", name="Mapped street lamp",
                          lat=12.975, lon=77.595, properties={"lit": "yes"}, provenance="live"))
        for idx, value in enumerate(("limited", "no", "designated", None)):
            db.add(GeoFeature(city_id="bengaluru", source_id="osm-overpass", layer="clinic",
                              external_id=f"test/wheelchair-{idx}", name="Mapped clinic",
                              lat=12.975, lon=77.595 + (idx + 1) * 0.0001,
                              properties={"wheelchair": value} if value else {}, provenance="cached"))

    async def fake_request_json(*_args, **_kwargs):
        return {"code": "Ok", "routes": [
            {"distance": 1450.0, "duration": 900.0, "geometry": {"type": "LineString", "coordinates": [[77.59, 12.97], [77.595, 12.975], [77.60, 12.98]]}},
            {"distance": 1800.0, "duration": 1050.0, "geometry": {"type": "LineString", "coordinates": [[77.59, 12.97], [77.59, 12.976], [77.60, 12.98]]}},
        ]}

    monkeypatch.setattr(routing, "request_json", fake_request_json)
    from swarmos.core.ratelimit import limiter

    limiter.reset()
    response = client.post("/api/v1/public/cities/bengaluru/routes/compare", json={
        "origin": {"lat": 12.97, "lon": 77.59}, "destination": {"lat": 12.98, "lon": 77.60}, "mode": "foot",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["routes"]) == 2
    assert data["routes"][0]["geometry"]["type"] == "LineString"
    assert "open_incident_points_within_250m" in data["routes"][0]["context"]
    assert data["routes"][0]["context"]["mapped_transit_stations_within_500m"] == {"bus_station": 1}
    assert data["routes"][0]["context"]["transit_features_checked"] >= 1
    assert data["routes"][0]["context"]["street_light_features_checked"] >= 1
    assert data["routes"][0]["context"]["mapped_street_lamp_points_within_100m"] == 1
    assert data["routes"][0]["context"]["mapped_wheelchair_accessibility_within_500m"] == {
        "yes": 1, "limited": 1, "no": 1, "designated": 1, "unknown": 1,
    }
    assert data["routes"][0]["context"]["wheelchair_features_near_route"] == 5
    evidence = data["routes"][0]["context"]["mapped_feature_evidence"]
    assert len(evidence) == 6 and not data["routes"][0]["context"]["mapped_feature_evidence_truncated"]
    assert evidence[0]["external_id"] == "test/bus-station-sakhi-route"
    assert evidence[0]["source_id"] == "osm-vector-tiles" and evidence[0]["wheelchair"] == "yes"
    assert any("transit counts" in item.lower() for item in data["limitations"])
    assert any("wheelchair values" in item.lower() for item in data["limitations"])
    assert any("street-lamp points" in item.lower() for item in data["limitations"])
    assert "safe" in " ".join(data["limitations"]).lower()
    assert "safety_score" not in data
    assert client.get("/api/v1/public/cities/bengaluru/routes").status_code == 404


def test_situation_report_persists_a_bounded_city_snapshot(operator):
    from swarmos.db import session_scope
    from swarmos.models import AgentExecution, AgentTask, InterventionOutcome, ProposedAction, ScenarioRun

    with session_scope() as db:
        action = ProposedAction(city_id="bengaluru", agent_id="mobility_optimization_agent", kind="review",
                               title="Review bus corridor timings", rationale="Mapped context supports a review.",
                               risk="low", required_role="operator", status="executed")
        db.add(action)
        db.flush()
        db.add(InterventionOutcome(action_id=action.id, city_id="bengaluru", outcome="unverified",
                                   note="Private operator note must not appear in the report.", measurement_ids=[]))
        task = AgentTask(city_id="bengaluru", prompt="synthetic report fixture", params={}, plan={},
                         status="succeeded", result={"summary": "Synthetic persisted task summary."})
        db.add(task)
        db.flush()
        db.add(ScenarioRun(city_id="bengaluru", task_id=task.id, name="Synthetic rainfall case",
                           params={"extra_rainfall_mm": 25, "temperature_delta_c": 0}))
        db.add(AgentExecution(task_id=task.id, agent_id="cross_domain_correlation_agent", status="succeeded",
                              output={"summary": "Synthetic co-occurrence review.", "findings": [], "metrics": {},
                                      "data_refs": [], "provenance": ["demo"], "limitations": ["Synthetic fixture."]}))

    response = operator.post("/api/v1/cities/bengaluru/reports")
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["snapshot"]["city"]["id"] == "bengaluru"
    assert report["snapshot"]["generated_at"]
    assert "indicators" in report["snapshot"] and "source_health" in report["snapshot"]
    assert "audit" not in report["snapshot"] and "sync_runs" not in report["snapshot"]
    snapshot = report["snapshot"]
    assert snapshot["recommendations"][0]["title"] == "Review bus corridor timings"
    assert snapshot["intervention_outcomes"][0]["note_included"] is False
    assert snapshot["scenario_runs"][0]["name"] == "Synthetic rainfall case"
    assert snapshot["agent_reviews"][0]["consensus"]["status"] == "insufficient_overlap"
    assert snapshot["cross_domain_impacts"][0]["summary"] == "Synthetic co-occurrence review."
    assert "Private operator note must not appear in the report." not in response.text
    detail = operator.get(f"/api/v1/cities/bengaluru/reports/{report['id']}")
    assert detail.status_code == 200 and detail.json()["snapshot"] == report["snapshot"]
    listing = operator.get("/api/v1/cities/bengaluru/reports").json()
    assert listing["total"] >= 1 and listing["items"][0]["id"] == report["id"]
    assert operator.get(f"/api/v1/cities/mumbai/reports/{report['id']}").status_code == 404


def test_forecast_evaluation_matches_only_prior_live_forecasts_and_observations(viewer):
    from datetime import timedelta

    from swarmos.db import session_scope, utcnow
    from swarmos.models import Measurement

    now = utcnow()
    valid_at = now - timedelta(hours=1)
    with session_scope() as db:
        db.add_all([
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="weather",
                        metric="evaluation_test_temperature", entity="evaluation-station", value=20.0, unit="C",
                        kind="forecast", observed_at=valid_at, fetched_at=now - timedelta(hours=2), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="weather",
                        metric="evaluation_test_temperature", entity="evaluation-station", value=22.0, unit="C",
                        kind="observation", observed_at=valid_at, fetched_at=now, provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="weather",
                        metric="evaluation_test_temperature", entity="evaluation-demo", value=999.0, unit="C",
                        kind="forecast", observed_at=valid_at, fetched_at=now - timedelta(hours=2), provenance="demo", meta={}),
        ])
    response = viewer.get("/api/v1/cities/bengaluru/forecast-evaluation?days=30")
    assert response.status_code == 200, response.text
    data = response.json()
    matched = [row for row in data["matches"] if row["metric"] == "evaluation_test_temperature"]
    assert len(matched) == 1
    assert matched[0]["forecast"] == 20.0 and matched[0]["observation"] == 22.0
    summary = next(item for item in data["summary"] if item["metric"] == "evaluation_test_temperature")
    assert summary["matched_count"] == 1 and summary["mean_absolute_error"] == 2.0


def test_impact_review_requires_repeated_signals_across_domains_and_tasks(viewer):
    from swarmos.db import session_scope
    from swarmos.models import AgentExecution, AgentTask

    with session_scope() as db:
        tasks = [AgentTask(city_id="bengaluru", prompt="Synthetic integration test task", status="succeeded", plan={})
                 for _ in range(2)]
        db.add_all(tasks)
        db.flush()
        db.add_all([
            AgentExecution(task_id=tasks[0].id, agent_id="flood_risk_analyst", status="succeeded", output={
                "metrics": {"review_test_signal": 0.42}, "findings": [{"title": "Shared review signal", "severity": "moderate"}],
                "provenance": ["live"], "data_refs": [{"source": "weather"}],
            }),
            AgentExecution(task_id=tasks[1].id, agent_id="water_quality_monitor", status="succeeded", output={
                "metrics": {"review_test_signal": 0.73}, "findings": [{"title": "SHARED review-signal", "severity": "high"}],
                "provenance": ["cached"], "data_refs": [{"source": "water"}],
            }),
        ])
    response = viewer.get("/api/v1/cities/bengaluru/impact-review?days=30")
    assert response.status_code == 200, response.text
    data = response.json()
    metric = next(row for row in data["metric_relationships"] if row["signal"] == "review_test_signal")
    assert set(metric["domains"]) == {"environment", "water"}
    finding = next(row for row in data["finding_relationships"] if row["signal"] == "shared review signal")
    assert finding["task_count"] == 2
    assert "co-occurrence only" in " ".join(data["limitations"])


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
    assert detail["consensus"]["status"] in ("differences_found", "no_material_difference_detected", "insufficient_overlap")
    assert "not proof that one agent is correct" in " ".join(detail["consensus"]["limitations"])
    assert detail["result"]["execution_policy"].startswith("Agents only analyse")
    assert detail["handoffs"], "handoffs between dependent agents must be recorded"
    assert detail["actions"], "demo leak / water-quality data should yield proposals"
    action = next(a for a in detail["actions"] if a["kind"] == "create_incident")
    assert action["status"] == "pending" and action["payload"]["_evidence"]["demo_basis"] is True
    evidence = action["payload"]["_evidence"]
    assert isinstance(evidence["data_refs"], list) and isinstance(evidence["limitations"], list)
    assert any(ref.get("record_ids") or ref.get("feature_ids") for ref in evidence["data_refs"])
    resolved = operator.get(f"/api/v1/cities/bengaluru/actions/{action['id']}/evidence")
    assert resolved.status_code == 200
    assert any(ref["records"] for ref in resolved.json()["references"])
    assert operator.get(f"/api/v1/cities/delhi/actions/{action['id']}/evidence").status_code == 404
    review = operator.get("/api/v1/cities/bengaluru/actions", params={"status": "pending", "limit": 50}).json()
    listed = next(row for row in review["items"] if row["id"] == action["id"])
    assert listed["task_id"] == task["id"] and listed["payload"]["_evidence"]["data_refs"] == evidence["data_refs"]

    dec = operator.post(f"/api/v1/cities/bengaluru/actions/{action['id']}/decision", json={"approve": True, "note": "verified"})
    assert dec.status_code == 200
    out = dec.json()
    if out["required_role"] == "operator":
        assert out["status"] == "executed" and out["execution_result"]["incident_id"]
        recorded = operator.put(f"/api/v1/cities/bengaluru/actions/{action['id']}/outcome", json={
            "outcome": "unverified", "note": "Follow-up has not been completed yet.", "measurement_ids": [],
        })
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["outcome"] == "unverified"
        assert "does not establish" in recorded.json()["interpretation"]
        action_detail = operator.get(f"/api/v1/cities/bengaluru/tasks/{task['id']}").json()
        assert next(a for a in action_detail["actions"] if a["id"] == action["id"])["outcome"]["id"] == recorded.json()["id"]
    twice = operator.post(f"/api/v1/cities/bengaluru/actions/{action['id']}/decision", json={"approve": True})
    assert twice.status_code == 409


def test_intervention_outcome_pairs_only_comparable_observations(operator):
    from datetime import timedelta

    from swarmos.db import session_scope, utcnow
    from swarmos.models import Measurement, ProposedAction

    boundary = utcnow()
    with session_scope() as db:
        action = ProposedAction(city_id="bengaluru", agent_id="mobility_optimization_agent", kind="manual",
                                title="Review corridor signal plan", status="executed", executed_at=boundary)
        db.add(action)
        db.flush()
        action_id = action.id
        measurements = [
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="mobility",
                        metric="learning_test_flow", entity="corridor-a", value=18, unit="vehicles/min",
                        kind="observation", observed_at=boundary - timedelta(hours=2), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="mobility",
                        metric="learning_test_flow", entity="corridor-a", value=20, unit="vehicles/min",
                        kind="observation", observed_at=boundary - timedelta(hours=1), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="mobility",
                        metric="learning_test_flow", entity="corridor-a", value=22, unit="vehicles/min",
                        kind="observation", observed_at=boundary + timedelta(hours=1), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="mobility",
                        metric="learning_test_flow", entity="corridor-a", value=24, unit="vehicles/min",
                        kind="observation", observed_at=boundary + timedelta(hours=2), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="osm-overpass", category="mobility",
                        metric="learning_test_flow", entity="corridor-a", value=900, unit="vehicles/min",
                        kind="observation", observed_at=boundary + timedelta(hours=1), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="mobility",
                        metric="learning_test_flow", entity="corridor-b", value=80, unit="vehicles/min",
                        kind="observation", observed_at=boundary - timedelta(hours=1), provenance="live", meta={}),
            Measurement(city_id="bengaluru", source_id="open-meteo-weather", category="mobility",
                        metric="learning_test_flow", entity="corridor-b", value=90, unit="vehicles/min",
                        kind="observation", observed_at=boundary + timedelta(hours=1), provenance="live", meta={}),
        ]
        db.add_all(measurements)
        db.flush()
        measurement_ids = [measurement.id for measurement in measurements]

    response = operator.put(f"/api/v1/cities/bengaluru/actions/{action_id}/outcome", json={
        "outcome": "unverified", "note": "Review observations after the signal plan change.",
        "measurement_ids": measurement_ids,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["change_comparison_status"] == "paired_measurements_found"
    assert len(data["observed_changes"]) == 2
    assert {change["entity"] for change in data["observed_changes"]} == {"corridor-a", "corridor-b"}
    assert all(change["source_id"] == "open-meteo-weather" for change in data["observed_changes"])
    change = next(change for change in data["observed_changes"] if change["entity"] == "corridor-a")
    assert change["before"]["measurement_id"] == measurement_ids[1]
    assert change["after"]["measurement_id"] == measurement_ids[2]
    assert change["absolute_change"] == 2 and change["relative_change_pct"] == 10
    assert next(change for change in data["observed_changes"] if change["entity"] == "corridor-b")["absolute_change"] == 10
    assert "does not estimate causal impact" in data["comparison_method"]


def test_action_evidence_resolves_redacted_city_scoped_service_request(operator):
    from swarmos.db import session_scope
    from swarmos.models import ProposedAction, ServiceRequest

    request_id = "a" * 32
    other_city_request_id = "b" * 32
    with session_scope() as db:
        db.add(ServiceRequest(id=request_id, city_id="bengaluru", text="Private streetlight report text",
                              category="streetlight", department="Electrical", status="open", ward="Ward 4",
                              lat=12.97, lon=77.59, provenance="public"))
        db.add(ServiceRequest(id=other_city_request_id, city_id="delhi", text="Other city request",
                              category="roads", status="open", provenance="public"))
        action = ProposedAction(city_id="bengaluru", agent_id="streetlight_maintenance_agent", kind="review",
                                title="Review streetlight reports", rationale="Aggregated reports need review.",
                                payload={"_evidence": {"data_refs": [{
                                    "type": "records", "kind": "service_requests", "n": 1,
                                    "provenance": ["public"], "request_ids": [request_id, other_city_request_id],
                                    "request_ids_truncated": False,
                                }]}})
        db.add(action)
        db.flush()
        action_id = action.id

    response = operator.get(f"/api/v1/cities/bengaluru/actions/{action_id}/evidence")
    assert response.status_code == 200, response.text
    ref = response.json()["references"][0]
    assert [row["id"] for row in ref["records"]] == [request_id]
    assert ref["unresolved_ids"] == [other_city_request_id]
    assert ref["records"][0]["category"] == "streetlight" and ref["records"][0]["status"] == "open"
    assert "text" not in ref["records"][0] and "lat" not in ref["records"][0] and "ward" not in ref["records"][0]


def test_action_evidence_resolves_redacted_city_scoped_incidents_and_requests(operator):
    from uuid import uuid4

    from swarmos.db import session_scope
    from swarmos.models import AgentExecution, AgentTask, Incident, ProposedAction, ServiceRequest

    request_id, other_city_request_id = uuid4().hex, uuid4().hex
    incident_id, other_city_incident_id = uuid4().hex, uuid4().hex
    with session_scope() as db:
        db.add(ServiceRequest(id=request_id, city_id="bengaluru", text="Private streetlight report text",
                              category="streetlight", department="Electrical", status="open", ward="Ward 4",
                              lat=12.97, lon=77.59, provenance="public"))
        db.add(ServiceRequest(id=other_city_request_id, city_id="delhi", text="Other city request",
                              category="roads", status="open", provenance="public"))
        db.add(Incident(id=incident_id, city_id="bengaluru", title="Private incident details", description="Private incident narrative",
                        category="road", severity="high", status="open", lat=12.97, lon=77.59, provenance="operator"))
        db.add(Incident(id=other_city_incident_id, city_id="delhi", title="Other city incident", category="road",
                        severity="moderate", status="open", provenance="operator"))
        task = AgentTask(city_id="bengaluru", prompt="Persisted traceability fixture", params={}, plan={}, status="succeeded")
        db.add(task)
        db.flush()
        execution = AgentExecution(task_id=task.id, agent_id="incident_review_agent", status="succeeded",
                                   output={"summary": "Redacted execution summary", "limitations": ["Test limitation."]})
        db.add(execution)
        db.flush()
        action = ProposedAction(city_id="bengaluru", agent_id="streetlight_maintenance_agent", kind="review",
                                title="Review streetlight reports", rationale="Aggregated records need review.",
                                payload={"_evidence": {"data_refs": [
                                    {"type": "records", "kind": "service_requests", "n": 1,
                                     "provenance": ["public"], "request_ids": [request_id, other_city_request_id]},
                                    {"type": "records", "kind": "incidents", "n": 1,
                                     "provenance": ["operator"], "incident_ids": [incident_id, other_city_incident_id]},
                                    {"type": "records", "kind": "agent_executions", "n": 1,
                                     "provenance": ["derived"], "execution_ids": [execution.id]},
                                ]}})
        db.add(action)
        db.flush()
        action_id = action.id

    response = operator.get(f"/api/v1/cities/bengaluru/actions/{action_id}/evidence")
    assert response.status_code == 200, response.text
    request_ref, incident_ref, execution_ref = response.json()["references"]
    assert [row["id"] for row in request_ref["records"]] == [request_id]
    assert request_ref["unresolved_ids"] == [other_city_request_id]
    assert request_ref["records"][0]["category"] == "streetlight" and request_ref["records"][0]["status"] == "open"
    assert "text" not in request_ref["records"][0] and "lat" not in request_ref["records"][0] and "ward" not in request_ref["records"][0]
    assert [row["id"] for row in incident_ref["records"]] == [incident_id]
    assert incident_ref["unresolved_ids"] == [other_city_incident_id]
    assert incident_ref["records"][0]["severity"] == "high"
    assert "description" not in incident_ref["records"][0] and "lat" not in incident_ref["records"][0]
    assert execution_ref["records"][0]["agent_id"] == "incident_review_agent"
    assert execution_ref["records"][0]["summary"] == "Redacted execution summary"


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
    city_sources = {s["id"]: s for s in operator.get("/api/v1/cities/bengaluru/integrations").json()["items"]}
    assert "last_success_at" in city_sources["open-meteo-weather"]
    assert city_sources["open-meteo-weather"]["freshness"]["state"] in {"fresh", "stale", "never"}
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
