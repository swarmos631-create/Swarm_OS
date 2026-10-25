"""Central registry of the 50 SwarmOS agents.

Each agent is a declarative :class:`AgentSpec` bound to a domain skill. Agents share one runtime; there are no
per-agent classes. To add an agent, append a spec here (and a skill if no existing one fits).
"""

from __future__ import annotations

from dataclasses import replace

from swarmos.agents.contracts import AgentSpec, ScenarioParams, TextParams
from swarmos.agents.skills import (
    civic,
    energy,
    environment,
    governance_cross,
    mobility,
    planning_health,
    safety,
    water,
)

S = AgentSpec

AGENTS: list[AgentSpec] = [
    # 1. Mobility & traffic
    S("traffic_flow_analyst", "Traffic Flow Analyst", "mobility", "Computes corridor congestion indices from speed telemetry.",
      ("traffic.congestion_index",), ("traffic", "congestion", "jam", "road speed", "corridor", "commute"),
      mobility.traffic_flow, data=("traffic",), output_metrics=("city_congestion_index",)),
    S("congestion_forecast_agent", "Congestion Forecast Agent", "mobility", "Forecasts city-mean corridor speed for the next 24 h.",
      ("traffic.forecast",), ("forecast traffic", "congestion forecast", "peak hour", "tomorrow traffic", "rush hour"),
      mobility.congestion_forecast, data=("traffic",), model="congestion_forecaster"),
    S("public_transit_analyst", "Public Transit Analyst", "mobility", "Assesses mapped transit-station coverage.",
      ("transit.coverage",), ("transit", "bus", "metro", "rail", "train", "bus station", "railway station", "metro station", "public transport"),
      mobility.public_transit, data=("public_infrastructure",)),
    S("road_incident_triage", "Road Incident Triage Agent", "mobility", "Ranks open road and mobility incidents.",
      ("incidents.triage.road",), ("road incident", "accident", "crash", "breakdown", "road closure"),
      mobility.road_incident_triage, data=("incidents",)),
    S("mobility_optimization_agent", "Mobility Optimization Agent", "mobility", "Proposes traffic interventions for review.",
      ("traffic.recommend",), ("optimise traffic", "optimize traffic", "reduce congestion", "signal timing", "diversion"),
      mobility.mobility_optimization, depends_on=("traffic_flow_analyst", "road_incident_triage")),
    # 2. Environment & climate
    S("air_quality_analyst", "Air Quality Analyst", "environment", "Computes the indicative Indian NAQI and dominant pollutant.",
      ("air.naqi",), ("air quality", "aqi", "pollution", "pm2.5", "pm10", "smog", "haze", "air", "breathe", "mask"),
      environment.air_quality, data=("air_quality", "air_quality_station"), output_metrics=("aqi", "category")),
    S("weather_impact_agent", "Weather Impact Agent", "environment", "Classifies 24 h rainfall, heat stress and wind using IMD classes.",
      ("weather.impact",), ("weather", "rain", "rainfall", "storm", "wind", "barish", "monsoon", "cyclone", "humid", "humidity", "umbrella", "cold", "weather forecast", "will it rain", "thunderstorm", "lightning"),
      environment.weather_impact, data=("weather",)),
    S("flood_risk_analyst", "Flood Risk Analyst", "environment", "Transparent additive flood-risk index from rainfall and reports.",
      ("flood.risk_index",), ("flood", "flooding", "inundation", "waterlogging", "monsoon"),
      environment.flood_risk, data=("weather", "service_requests", "incidents"), output_metrics=("score", "level")),
    S("urban_heat_analyst", "Urban Heat Analyst", "environment", "Screens forecasts against the IMD absolute heatwave criterion.",
      ("heat.screening",), ("heat", "heatwave", "temperature", "hot", "heat stress", "garmi"),
      environment.urban_heat, data=("weather",)),
    S("environmental_anomaly_agent", "Environmental Anomaly Agent", "environment", "Robust z-score anomaly tests on environmental series.",
      ("environment.anomaly",), ("anomaly", "unusual", "spike", "abnormal reading"),
      environment.environmental_anomaly, data=("air_quality", "weather")),
    # 3. Water & sanitation
    S("water_demand_analyst", "Water Demand Analyst", "water", "Forecasts 24 h city water demand.",
      ("water.demand_forecast",), ("water demand", "water consumption", "supply planning", "pani"),
      water.water_demand, data=("water",), model="water_demand_forecaster"),
    S("water_quality_monitor", "Water Quality Monitor", "water", "Checks pH, turbidity and residual chlorine against IS 10500.",
      ("water.quality",), ("water quality", "contamination", "turbidity", "chlorine", "drinking water"),
      water.water_quality, data=("water_quality",)),
    S("leak_detection_agent", "Leak Detection Agent", "water", "Minimum-night-flow leak screening per DMA.",
      ("water.leak",), ("leak", "leakage", "pipe burst", "non-revenue water", "nrw"),
      water.leak_detection, data=("water",)),
    S("drainage_risk_agent", "Drainage Risk Agent", "water", "Finds drainage complaint hotspots in the context of flood risk.",
      ("drainage.hotspots",), ("drain", "drainage", "nala", "storm water", "desilting"),
      water.drainage_risk, data=("service_requests",), depends_on=("flood_risk_analyst",)),
    S("wastewater_operations_agent", "Wastewater Operations Agent", "water", "STP utilisation and effluent BOD checks.",
      ("wastewater.ops",), ("sewage", "wastewater", "stp", "sewerage", "effluent"),
      water.wastewater, data=("wastewater",), config={"bod_limit_mgl": 20.0}),
    # 4. Energy & utilities
    S("energy_demand_forecaster", "Energy Demand Forecaster", "energy", "Forecasts 24 h electricity demand.",
      ("energy.demand_forecast",), ("electricity demand", "power demand", "load forecast", "peak demand", "bijli"),
      energy.energy_demand, data=("energy",), model="energy_demand_forecaster"),
    S("grid_anomaly_agent", "Grid Anomaly Agent", "energy", "Frequency/voltage rule checks plus an Isolation Forest outlier model.",
      ("energy.grid_anomaly",), ("grid", "frequency", "voltage", "feeder", "transformer"),
      energy.grid_anomaly, data=("energy",), model="sensor_anomaly_detector"),
    S("renewable_energy_optimizer", "Renewable Energy Optimizer", "energy", "Solar yield per kWp from irradiance forecasts.",
      ("energy.solar",), ("solar", "renewable", "irradiance", "rooftop"),
      energy.renewable_optimizer, data=("weather",), config={"performance_ratio": 0.75}),
    S("streetlight_operations_agent", "Streetlight Operations Agent", "energy", "Astronomical switching schedule and fault backlog.",
      ("energy.streetlights",), ("streetlight", "street light", "lighting", "lamp"),
      energy.streetlights, data=("weather", "service_requests"), config={"switch_offset_min": 10}),
    S("utility_outage_triage_agent", "Utility Outage Triage Agent", "energy", "Ranks open power/water outage incidents.",
      ("utility.outage_triage",), ("outage", "power cut", "blackout", "no electricity", "no water"),
      energy.outage_triage, data=("incidents",)),
    # 5. Public safety & emergency
    S("emergency_triage", "Emergency Triage Agent", "safety", "Weighted priority ranking of all open incidents.",
      ("incidents.triage",), ("emergency", "incident", "triage", "critical", "fire", "rescue"),
      safety.emergency_triage, data=("incidents",), output_metrics=("open",)),
    S("incident_correlation_agent", "Incident Correlation Agent", "safety", "Spatio-temporal clustering of incidents.",
      ("incidents.correlation",), ("correlate", "cluster", "related incidents", "pattern"),
      safety.incident_correlation, data=("incidents",), config={"radius_km": 1.0, "window_h": 6}),
    S("emergency_resource_coordinator", "Emergency Resource Coordinator", "safety", "Nearest mapped facilities for priority incidents.",
      ("incidents.resources",), ("dispatch", "ambulance", "fire station", "nearest hospital", "response"),
      safety.resource_coordinator, data=("public_infrastructure",), depends_on=("emergency_triage",)),
    S("disaster_impact_analyst", "Disaster Impact Analyst", "safety", "Screens seismic and extreme-weather hazards.",
      ("hazards.screening",), ("disaster", "earthquake", "hazard", "landslide", "impact", "alert", "warning", "official alert", "ndma", "gdacs"),
      safety.disaster_impact, data=("seismic",), depends_on=("weather_impact_agent", "flood_risk_analyst"), config={"radius_km": 500}),
    S("public_safety_situation_summarizer", "Public Safety Situation Summarizer", "safety", "Rolls up safety analyses into a brief.",
      ("safety.summary",), ("safety summary", "public safety", "law and order", "situation report"),
      safety.safety_summarizer, depends_on=("emergency_triage", "incident_correlation_agent", "disaster_impact_analyst")),
    # 6. Civic services
    S("complaint_classification_agent", "Complaint Classification Agent", "civic", "Classifies citizen complaints (trained text model).",
      ("civic.classify",), ("complaint", "grievance", "classify", "citizen request", "garbage", "trash", "waste", "pothole", "potholes", "stray dog", "noise"),
      civic.complaint_classification, data=("service_requests",), params=TextParams, model="complaint_classifier"),
    S("service_request_routing_agent", "Service Request Routing Agent", "civic", "Routes requests to departments.",
      ("civic.route",), ("route", "department", "assign", "which department"),
      civic.service_routing, data=("service_requests",), depends_on=("complaint_classification_agent",)),
    S("civic_resolution_tracker", "Civic Resolution Tracker", "civic", "SLA compliance by complaint category.",
      ("civic.sla",), ("sla", "resolution", "pending complaints", "backlog", "overdue"),
      civic.resolution_tracker, data=("service_requests",)),
    S("public_asset_maintenance_agent", "Public Asset Maintenance Agent", "civic", "Maintenance backlog from asset condition.",
      ("assets.maintenance",), ("maintenance", "repair", "asset", "bridge", "inspection"),
      civic.asset_maintenance, data=("assets", "service_requests")),
    S("service_quality_analyst", "Service Quality Analyst", "civic", "Resolution-time trends.",
      ("civic.quality",), ("service quality", "resolution time", "performance of services", "citizen satisfaction"),
      civic.service_quality, data=("service_requests",)),
    # 7. Urban planning & infrastructure
    S("land_use_analysis_agent", "Land Use Analysis Agent", "planning", "Mapped land-use mix from OSM.",
      ("planning.land_use",), ("land use", "zoning", "residential", "commercial", "industrial"),
      planning_health.land_use, data=("land_use",)),
    S("infrastructure_health_analyst", "Infrastructure Health Analyst", "planning", "Asset condition bands.",
      ("assets.health",), ("infrastructure health", "condition", "structural", "ageing infrastructure"),
      planning_health.infrastructure_health, data=("assets",)),
    S("construction_impact_agent", "Construction Impact Agent", "planning", "Construction-related disruption from reports.",
      ("planning.construction",), ("construction", "metro work", "digging", "excavation", "debris"),
      planning_health.construction_impact, data=("incidents", "service_requests"), depends_on=("traffic_flow_analyst",)),
    S("urban_growth_analyst", "Urban Growth Analyst", "planning", "Built-up area growth from multi-year land cover.",
      ("planning.growth",), ("urban growth", "sprawl", "expansion", "built-up"),
      planning_health.urban_growth, data=("urban_growth",)),
    S("infrastructure_prioritization_agent", "Infrastructure Prioritization Agent", "planning", "Multi-criteria capital-works ranking.",
      ("assets.prioritise",), ("prioritise", "prioritize", "capital works", "investment priority"),
      planning_health.infrastructure_prioritization,
      depends_on=("infrastructure_health_analyst", "public_asset_maintenance_agent", "civic_resolution_tracker")),
    # 8. Public health & community
    S("aggregate_health_trend_analyst", "Aggregate Health Trend Analyst", "health", "Week-on-week aggregated surveillance trends with small-cell suppression.",
      ("health.trends",), ("health trend", "disease", "dengue", "malaria", "outbreak", "cases"),
      planning_health.aggregate_health, data=("health_aggregate",), min_role="operator"),
    S("facility_capacity_planner", "Facility Capacity Planner", "health", "Health facility counts and density.",
      ("health.facilities",), ("hospital", "clinic", "health facility", "beds", "capacity", "doctor", "doctors", "healthcare"),
      planning_health.facility_capacity, data=("public_infrastructure",)),
    S("community_needs_analyst", "Community Needs Analyst", "health", "Community concerns from service requests.",
      ("community.needs",), ("community", "needs", "residents", "ward issues"),
      planning_health.community_needs, data=("service_requests",)),
    S("accessibility_assessment_agent", "Accessibility Assessment Agent", "health", "Wheelchair-accessibility tagging of facilities.",
      ("community.accessibility",), ("accessibility", "wheelchair", "disabled", "divyang", "barrier-free"),
      planning_health.accessibility, data=("public_infrastructure",)),
    S("public_service_coverage_agent", "Public Service Coverage Agent", "health", "Grid coverage by distance to facilities.",
      ("community.coverage",), ("coverage", "access to services", "service gap", "underserved", "police station", "police", "school", "schools", "nearest", "how far"),
      planning_health.service_coverage, data=("public_infrastructure",)),
    # 9. Governance & operations
    S("policy_document_analyst", "Policy Document Analyst", "governance", "Retrieves relevant passages from authorised documents.",
      ("knowledge.retrieve",), ("policy", "sop", "guideline", "regulation", "procedure", "manual", "action plan"),
      governance_cross.policy_documents, data=("documents",), params=TextParams),
    S("compliance_checklist_agent", "Compliance Checklist Agent", "governance", "Checks every proposed action against approval rules.",
      ("governance.compliance",), ("compliance", "checklist", "approval", "audit"),
      governance_cross.compliance_checklist, depends_on=("city_operations_planning_agent",), aggregate=True),
    S("budget_analysis_agent", "Budget Analysis Agent", "governance", "Allocation vs expenditure by department.",
      ("governance.budget",), ("budget", "expenditure", "spending", "allocation", "finance"),
      governance_cross.budget_analysis, data=("budget",), min_role="operator"),
    S("operational_performance_agent", "Operational Performance Agent", "governance", "Agent, sync and incident KPIs.",
      ("governance.kpis",), ("kpi", "operational performance", "mttr", "uptime", "efficiency"),
      governance_cross.operational_performance, data=("internal",)),
    S("resource_allocation_agent", "Resource Allocation Agent", "governance", "Greedy crew-to-incident allocation.",
      ("governance.allocation",), ("allocate", "resources", "crews", "staffing", "deploy"),
      governance_cross.resource_allocation, data=("resources",), depends_on=("emergency_triage",)),
    # 10. Cross-domain intelligence
    S("city_situation_summarizer", "City Situation Summarizer", "cross", "Structured roll-up of domain analyses into a situation brief.",
      ("city.summary",), ("summary", "overview", "situation", "brief", "what is happening", "how is the city", "city status", "everything", "full report"),
      governance_cross.city_summary,
      depends_on=("air_quality_analyst", "weather_impact_agent", "emergency_triage", "traffic_flow_analyst",
                  "utility_outage_triage_agent", "flood_risk_analyst")),
    S("cross_domain_correlation_agent", "Cross-Domain Correlation Agent", "cross", "Pearson correlations across domain series.",
      ("city.correlation",), ("correlation", "relationship", "link between", "cross-domain"),
      governance_cross.cross_correlation, data=("air_quality", "weather", "traffic")),
    S("anomaly_investigation_agent", "Anomaly Investigation Agent", "cross", "Consolidates anomaly detector outputs.",
      ("city.anomalies",), ("investigate", "anomalies", "root cause", "what is abnormal"),
      governance_cross.anomaly_investigation,
      depends_on=("environmental_anomaly_agent", "grid_anomaly_agent", "leak_detection_agent")),
    S("scenario_simulation_agent", "Scenario Simulation Agent", "cross", "Deterministic what-if on rainfall and temperature.",
      ("city.scenario",), ("scenario", "what if", "simulate", "simulation", "stress test"),
      governance_cross.scenario_simulation, data=("weather",), params=ScenarioParams),
    S("city_operations_planning_agent", "City Operations Planning Agent", "cross", "Consolidates every proposed action into a time-horizon plan.",
      ("city.plan",), ("plan", "action plan", "operations plan", "what should we do", "recommend"),
      governance_cross.operations_planning, aggregate=True),
]

# Agents backed by a trained model need longer on a small shared CPU when a whole swarm runs at once.
MODEL_AGENT_TIMEOUT_S = 45.0
AGENTS = [replace(a, timeout_s=a.timeout_s or MODEL_AGENT_TIMEOUT_S) if a.model else a for a in AGENTS]

REGISTRY: dict[str, AgentSpec] = {a.id: a for a in AGENTS}


def validate_registry() -> None:
    if len(AGENTS) != 50:
        raise AssertionError(f"Expected 50 agents, found {len(AGENTS)}")
    if len(REGISTRY) != len(AGENTS):
        raise AssertionError("Duplicate agent ids")
    names = [a.name for a in AGENTS]
    if len(set(names)) != len(names):
        raise AssertionError("Duplicate agent names")
    from collections import Counter

    per_domain = Counter(a.domain for a in AGENTS)
    if set(per_domain.values()) != {5} or len(per_domain) != 10:
        raise AssertionError(f"Each of 10 domains must have 5 agents: {dict(per_domain)}")
    for a in AGENTS:
        for d in a.depends_on:
            if d not in REGISTRY:
                raise AssertionError(f"{a.id} depends on unknown agent {d}")
    # cycle check over explicit dependencies
    state: dict[str, int] = {}

    def visit(n: str) -> None:
        if state.get(n) == 1:
            raise AssertionError(f"Dependency cycle at {n}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in REGISTRY[n].depends_on:
            visit(d)
        state[n] = 2

    for a in AGENTS:
        visit(a.id)


validate_registry()
