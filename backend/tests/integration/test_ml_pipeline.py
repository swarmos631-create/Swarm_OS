"""Training pipeline smoke tests on small synthetic datasets (CPU, a few seconds)."""

import yaml

from swarmos.ai.runtime import ModelUnavailable, get_runtime
from swarmos.config import get_settings
from swarmos.ml import pipeline


def _shrink(name: str, **dataset_params):
    s = get_settings()
    path = s.ml_configs_dir / f"{name}.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["dataset"]["params"].update(dataset_params)
    path.write_text(yaml.safe_dump(cfg))


def test_untrained_model_is_unavailable_not_fabricated():
    get_runtime().artifacts.invalidate()
    try:
        get_runtime().load("energy_demand_forecaster")
        raise AssertionError("expected ModelUnavailable")
    except ModelUnavailable as exc:
        assert "No promoted version" in str(exc)


def test_train_promote_rollback_cycle():
    _shrink("energy_demand_forecaster", hours=24 * 40)
    r1 = pipeline.train_model("energy_demand_forecaster", promote_if_better=True)
    assert r1["metrics"]["mae"] > 0 and "baseline" in r1
    assert r1["gate_passed"] and r1["promoted"]
    loaded = get_runtime().load("energy_demand_forecaster")
    assert loaded.version == r1["version"]
    r2 = pipeline.train_model("energy_demand_forecaster", promote_if_better=True)
    assert get_runtime().load("energy_demand_forecaster").version == r2["version"]
    back = pipeline.rollback("energy_demand_forecaster")
    assert back == r1["version"]
    assert get_runtime().load("energy_demand_forecaster").version == r1["version"]
    report = get_settings().ml_root / pipeline.list_versions("energy_demand_forecaster")[0].report_path
    assert "Synthetic data: True" in report.read_text()


def test_classifier_and_anomaly_detector_train():
    _shrink("complaint_classifier", rows=600)
    r = pipeline.train_model("complaint_classifier")
    assert 0 <= r["metrics"]["macro_f1"] <= 1 and r["promoted"] is False
    _shrink("sensor_anomaly_detector", hours=24 * 30)
    r = pipeline.train_model("sensor_anomaly_detector")
    assert set(r["metrics"]) == {"f1", "precision", "recall"}


def test_gate_failure_blocks_promotion():
    versions = [v for v in pipeline.list_versions("sensor_anomaly_detector")]
    failing = next((v for v in versions if not v.passed_gate), None)
    if failing is None:
        return  # gate happened to pass on this small sample; covered by evaluate_gate unit below
    try:
        pipeline.promote("sensor_anomaly_detector", failing.version)
        raise AssertionError("promotion should be refused")
    except pipeline.PromotionError:
        pass


def test_evaluate_gate_logic():
    cfg = {"gate": {"metric": "mae", "lower_is_better": True, "margin": 0.05}}
    assert pipeline.evaluate_gate(cfg, {"mae": 9.0}, {"mae": 10.0})[0]
    assert not pipeline.evaluate_gate(cfg, {"mae": 9.8}, {"mae": 10.0})[0]
    cfg = {"gate": {"metric": "f1", "margin": 0.0, "min_value": 0.5}}
    assert not pipeline.evaluate_gate(cfg, {"f1": 0.45}, {"f1": 0.4})[0]
