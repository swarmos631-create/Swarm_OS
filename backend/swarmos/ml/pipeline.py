"""Reproducible training, evaluation, registration, promotion and rollback.

Usage (CLI):  swarmos ml train complaint_classifier [--promote-if-better]
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import yaml
from sklearn.ensemble import HistGradientBoostingRegressor, IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, precision_recall_fscore_support
from sklearn.pipeline import FeatureUnion, Pipeline
from sqlalchemy import select

from swarmos.ai.runtime import get_runtime, sha256_file
from swarmos.config import Settings, get_settings
from swarmos.db import session_scope, utcnow
from swarmos.ml import datasets
from swarmos.ml.complaints import KeywordLexiconFeatures, rule_classify
from swarmos.ml.features import (
    ANOMALY_FEATURES,
    FORECAST_FEATURES,
    anomaly_features,
    forecast_row,
    hourly_map,
    seasonal_naive,
)
from swarmos.models import AuditLog, ModelVersion

log = logging.getLogger("swarmos.ml")


class PromotionError(Exception):
    pass


def load_config(settings: Settings, model_name: str) -> dict[str, Any]:
    path = settings.ml_configs_dir / f"{model_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No training config for '{model_name}' at {path}")
    cfg = yaml.safe_load(path.read_text())
    if cfg.get("model_name") != model_name:
        raise ValueError("model_name in config does not match file name")
    return cfg


def available_configs(settings: Settings) -> list[dict[str, Any]]:
    return [yaml.safe_load(p.read_text()) for p in sorted(settings.ml_configs_dir.glob("*.yaml"))]


def ensure_dataset(settings: Settings, cfg: dict[str, Any]) -> datasets.Dataset:
    ds_cfg = cfg["dataset"]
    rel = f"synthetic/{ds_cfg['name']}"
    manifest = settings.datasets_dir / rel / "manifest.json"
    if manifest.exists():
        existing = json.loads(manifest.read_text())
        if existing.get("generator", {}).get("params") == ds_cfg["params"]:
            return datasets.load(settings.datasets_dir, rel)
    return datasets.generate(settings.datasets_dir, ds_cfg["name"], ds_cfg["generator"], ds_cfg["params"], ds_cfg["schema"])


def _seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


# ----------------------------------------------------------------------------- trainers

def train_text_classifier(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    split = cfg["split"]
    rng = random.Random(split["seed"])
    train, val, test = [], [], []
    if split.get("method") == "template_holdout":
        # Hold out one whole template per category so the test set measures generalisation to unseen phrasing.
        by_cat: dict[str, set[str]] = {}
        for r in rows:
            by_cat.setdefault(r["category"], set()).add(r["template_id"])
        held = {cat: sorted(tids)[-1] for cat, tids in by_cat.items()}
        for r in rows:
            if r["template_id"] == held[r["category"]]:
                test.append(r)
            elif rng.random() < split["val"]:
                val.append(r)
            else:
                train.append(r)
    else:
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r["category"], []).append(r)
        for items in groups.values():  # stratified random split
            items = items[:]
            rng.shuffle(items)
            n = len(items)
            n_test, n_val = int(n * split["test"]), int(n * split["val"])
            test += items[:n_test]
            val += items[n_test : n_test + n_val]
            train += items[n_test + n_val :]
    hp = cfg["hyperparams"]
    est = Pipeline([
        ("tfidf", FeatureUnion([
            ("word", TfidfVectorizer(ngram_range=(1, int(hp.get("ngram_max", 2))), min_df=int(hp.get("min_df", 1)), sublinear_tf=True)),
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, sublinear_tf=True)),
            ("lexicon", KeywordLexiconFeatures()),
        ])),
        ("clf", LogisticRegression(C=float(hp.get("C", 4.0)), max_iter=2000, random_state=split["seed"])),
    ])
    est.fit([r["text"] for r in train], [r["category"] for r in train])
    y_true = [r["category"] for r in test]
    y_pred = list(est.predict([r["text"] for r in test]))
    y_base = [rule_classify(r["text"])[0] for r in test]
    val_pred = list(est.predict([r["text"] for r in val]))
    return {
        "estimator": est,
        "feature_spec": {"input": "text"},
        "metrics": {"macro_f1": round(f1_score(y_true, y_pred, average="macro"), 4), "accuracy": round(accuracy_score(y_true, y_pred), 4),
                    "val_macro_f1": round(f1_score([r["category"] for r in val], val_pred, average="macro"), 4)},
        "baseline": {"name": "keyword_rules", "macro_f1": round(f1_score(y_true, y_base, average="macro"), 4),
                     "accuracy": round(accuracy_score(y_true, y_base), 4)},
        "split_sizes": {"train": len(train), "val": len(val), "test": len(test)},
        "per_class_support": dict(Counter(y_true)),
    }


def train_forecaster(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    times = [datetime.fromisoformat(r["timestamp"]) for r in rows]
    vals = [float(r["value"]) for r in rows]
    series = hourly_map(times, vals)
    split = cfg["split"]
    end = max(series)
    test_start = end - timedelta(hours=24 * split["test_days"])
    val_start = test_start - timedelta(hours=24 * split["val_days"])
    X_tr, y_tr, X_va, y_va, X_te, y_te, base_te = [], [], [], [], [], [], []
    for t in sorted(series):
        row = forecast_row(series, t)
        if row is None:
            continue
        if t >= test_start:
            X_te.append(row)
            y_te.append(series[t])
            base_te.append(seasonal_naive(series, t, 168))
        elif t >= val_start:
            X_va.append(row)
            y_va.append(series[t])
        else:
            X_tr.append(row)
            y_tr.append(series[t])
    hp = cfg["hyperparams"]
    est = HistGradientBoostingRegressor(max_iter=int(hp.get("max_iter", 300)), learning_rate=float(hp.get("learning_rate", 0.05)),
                                        max_leaf_nodes=int(hp.get("max_leaf_nodes", 31)), random_state=split["seed"])
    est.fit(np.array(X_tr), np.array(y_tr))
    pred = est.predict(np.array(X_te))
    yt = np.array(y_te)
    base = np.array(base_te, dtype=float)

    def mape(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.mean(np.abs((a - b) / np.where(a == 0, 1, a))))

    return {
        "estimator": est,
        "feature_spec": {"features": list(FORECAST_FEATURES), "horizon_h": 24, "target_unit": cfg.get("unit")},
        "metrics": {"mae": round(float(mean_absolute_error(yt, pred)), 4), "mape": round(mape(yt, pred), 4),
                    "val_mae": round(float(mean_absolute_error(np.array(y_va), est.predict(np.array(X_va)))), 4)},
        "baseline": {"name": "seasonal_naive_168h", "mae": round(float(mean_absolute_error(yt, base)), 4), "mape": round(mape(yt, base), 4)},
        "split_sizes": {"train": len(X_tr), "val": len(X_va), "test": len(X_te)},
    }


def train_anomaly_detector(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    X = np.array([anomaly_features(float(r["frequency_hz"]), float(r["voltage_pu"]), float(r["load_pct"]), float(r["demand_delta_pct"]))
                  for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    n = len(rows)
    split = cfg["split"]
    cut = int(n * (1 - split["test"]))
    X_tr, X_te, y_te = X[:cut], X[cut:], y[cut:]
    hp = cfg["hyperparams"]
    est = IsolationForest(n_estimators=int(hp.get("n_estimators", 200)), contamination=float(hp.get("contamination", 0.03)),
                          random_state=split["seed"])
    est.fit(X_tr[y[:cut] == 0] if hp.get("train_on_normal_only", True) else X_tr)
    pred = (est.predict(X_te) == -1).astype(int)
    p, r, f, _ = precision_recall_fscore_support(y_te, pred, average="binary", zero_division=0)
    # baseline: fixed engineering bands
    base = np.array([int(not (49.90 <= 50 + x[0] <= 50.05) or not (0.95 <= 1 + x[1] <= 1.05) or x[2] > 1.0) for x in X_te])
    bp, br, bf, _ = precision_recall_fscore_support(y_te, base, average="binary", zero_division=0)
    return {
        "estimator": est,
        "feature_spec": {"features": list(ANOMALY_FEATURES), "output": "-1 anomaly / 1 normal"},
        "metrics": {"f1": round(float(f), 4), "precision": round(float(p), 4), "recall": round(float(r), 4)},
        "baseline": {"name": "engineering_bands", "f1": round(float(bf), 4), "precision": round(float(bp), 4), "recall": round(float(br), 4)},
        "split_sizes": {"train": cut, "test": n - cut, "test_anomalies": int(y_te.sum())},
    }


TRAINERS = {"text_classification": train_text_classifier, "forecasting": train_forecaster, "anomaly_detection": train_anomaly_detector}


def evaluate_gate(cfg: dict[str, Any], metrics: dict[str, Any], baseline: dict[str, Any]) -> tuple[bool, str]:
    g = cfg["gate"]
    m = g["metric"]
    lower_better = g.get("lower_is_better", False)
    value, base = metrics[m], baseline[m]
    margin = float(g.get("margin", 0))
    if lower_better:
        ok = value <= base * (1 - margin) and value <= g.get("max_value", float("inf"))
        msg = f"{m}={value} vs baseline {base} (needs ≤ {base * (1 - margin):.4f})"
    else:
        ok = value >= base + margin and value >= g.get("min_value", float("-inf"))
        msg = f"{m}={value} vs baseline {base} (needs ≥ {base + margin:.4f}, min {g.get('min_value')})"
    return ok, msg


# ----------------------------------------------------------------------------- orchestration

def train_model(model_name: str, *, job_id: str | None = None, promote_if_better: bool = False, actor_id: str | None = None,
                settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    cfg = load_config(settings, model_name)
    _seed(int(cfg["split"]["seed"]))
    t0 = time.monotonic()
    ds = ensure_dataset(settings, cfg)
    validation = datasets.validate(ds)
    out = TRAINERS[cfg["task_type"]](ds.rows, cfg)
    gate_ok, gate_msg = evaluate_gate(cfg, out["metrics"], out["baseline"])

    fingerprint = hashlib.sha256(json.dumps({"cfg": cfg, "data": ds.manifest["sha256"]}, sort_keys=True).encode()).hexdigest()[:8]
    version = datetime.now(UTC).strftime("v%Y%m%dT%H%M%SZ") + f"-{fingerprint}-{secrets.token_hex(2)}"
    art_dir = settings.artifacts_dir / model_name / version
    art_dir.mkdir(parents=True, exist_ok=True)
    art_path = art_dir / "model.joblib"
    joblib.dump({"estimator": out["estimator"], "feature_spec": out["feature_spec"], "model_name": model_name, "version": version}, art_path)
    digest = sha256_file(art_path)
    duration = round(time.monotonic() - t0, 2)
    meta = {
        "model_name": model_name, "version": version, "task_type": cfg["task_type"], "algorithm": cfg["algorithm"],
        "metrics": out["metrics"], "baseline": out["baseline"], "gate": {"passed": gate_ok, "detail": gate_msg},
        "dataset": {k: ds.manifest[k] for k in ("name", "sha256", "rows", "synthetic", "license", "provenance")},
        "validation": validation, "split_sizes": out["split_sizes"], "config": cfg, "artifact_sha256": digest,
        "training_seconds": duration, "trained_at": datetime.now(UTC).isoformat(),
        "environment": {"sklearn": __import__("sklearn").__version__, "numpy": np.__version__, "device": "cpu"},
    }
    (art_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    report_path = settings.reports_dir / model_name / f"{version}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_md = _report_md(meta)
    report_path.write_text(report_md)

    with session_scope() as db:
        mv = ModelVersion(model_name=model_name, version=version, task_type=cfg["task_type"], algorithm=cfg["algorithm"],
                          artifact_path=str(art_path.relative_to(settings.ml_root)), artifact_sha256=digest,
                          metrics=out["metrics"], baseline_metrics=out["baseline"], dataset_manifest=meta["dataset"],
                          config=cfg, report_path=str(report_path.relative_to(settings.ml_root)), passed_gate=gate_ok, job_id=job_id,
                          artifact_blob=art_path.read_bytes(), report_md=report_md)
        db.add(mv)
        db.add(AuditLog(action="model.train", actor_id=actor_id, target_type="model_version", target_id=f"{model_name}@{version}",
                        details={"gate": gate_ok, "metrics": out["metrics"]}))
    promoted = False
    if promote_if_better and gate_ok:
        promote(model_name, version, actor_id=actor_id, settings=settings)
        promoted = True
    log.info("model_trained", extra={"model": model_name, "version": version, "gate": gate_ok, "seconds": duration})
    return {"model_name": model_name, "version": version, "metrics": out["metrics"], "baseline": out["baseline"],
            "gate_passed": gate_ok, "gate": gate_msg, "promoted": promoted, "training_seconds": duration}


def promote(model_name: str, version: str, *, actor_id: str | None = None, force: bool = False, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with session_scope() as db:
        mv = db.scalar(select(ModelVersion).where(ModelVersion.model_name == model_name, ModelVersion.version == version))
        if mv is None:
            raise PromotionError("Unknown model version")
        if not mv.passed_gate and not force:
            raise PromotionError("Version did not pass its evaluation gate; promotion requires force by an admin")
        if artifact_bytes(mv, settings) is None:
            raise PromotionError("Artifact missing or checksum mismatch; refusing to promote")
        for other in db.scalars(select(ModelVersion).where(ModelVersion.model_name == model_name, ModelVersion.status == "promoted")):
            other.status = "archived"
        mv.status, mv.promoted_at, mv.promoted_by = "promoted", utcnow(), actor_id
        db.add(AuditLog(action="model.promote", actor_id=actor_id, target_type="model_version", target_id=f"{model_name}@{version}",
                        details={"forced": force}))
    get_runtime().artifacts.invalidate(model_name)


def artifact_bytes(mv: ModelVersion, settings: Settings) -> bytes | None:
    """Artifact content whose SHA-256 matches the registry: the database copy first, then the local file."""
    candidates: list[bytes] = []
    if mv.artifact_blob:
        candidates.append(mv.artifact_blob)
    path = Path(mv.artifact_path)
    path = path if path.is_absolute() else settings.ml_root / path
    if path.exists():
        candidates.append(path.read_bytes())
    for data in candidates:
        if hashlib.sha256(data).hexdigest() == mv.artifact_sha256:
            return data
    return None


def rollback(model_name: str, *, actor_id: str | None = None, settings: Settings | None = None) -> str:
    """Re-promote the most recently promoted *previous* version."""
    with session_scope() as db:
        current = db.scalar(select(ModelVersion).where(ModelVersion.model_name == model_name, ModelVersion.status == "promoted"))
        q = select(ModelVersion).where(ModelVersion.model_name == model_name, ModelVersion.promoted_at.is_not(None),
                                       ModelVersion.status != "promoted").order_by(ModelVersion.promoted_at.desc())
        prev = db.scalars(q).first()
        if prev is None:
            raise PromotionError("No previously promoted version to roll back to")
        target = prev.version
        _ = current
    promote(model_name, target, actor_id=actor_id, force=True, settings=settings)
    with session_scope() as db:
        db.add(AuditLog(action="model.rollback", actor_id=actor_id, target_type="model", target_id=model_name, details={"to": target}))
    return target


def _report_md(m: dict[str, Any]) -> str:
    rows = "\n".join(f"| {k} | {v} | {m['baseline'].get(k, '—')} |" for k, v in m["metrics"].items())
    return f"""# Evaluation report — {m['model_name']} {m['version']}

* Task: {m['task_type']} · Algorithm: {m['algorithm']} · Device: CPU · Training time: {m['training_seconds']} s
* Dataset: `{m['dataset']['name']}` ({m['dataset']['rows']} rows, sha256 `{m['dataset']['sha256'][:16]}…`)
* **Synthetic data: {m['dataset']['synthetic']}** — {m['dataset']['provenance']}
* Split sizes: {m['split_sizes']}
* Baseline: {m['baseline'].get('name')}
* Promotion gate: **{'PASSED' if m['gate']['passed'] else 'FAILED'}** — {m['gate']['detail']}

| Metric | Model | Baseline |
|---|---|---|
{rows}

## Limitations
Trained and evaluated on synthetic data only. These metrics demonstrate that the pipeline works end to end;
they say nothing about real-world accuracy. Retrain and re-evaluate on authorised, representative city data
before relying on this model operationally.
"""


def list_versions(model_name: str | None = None) -> list[ModelVersion]:
    with session_scope() as db:
        q = select(ModelVersion).order_by(ModelVersion.created_at.desc())
        if model_name:
            q = q.where(ModelVersion.model_name == model_name)
        return list(db.scalars(q))


def artifact_dir(settings: Settings, model_name: str, version: str) -> Path:
    return settings.artifacts_dir / model_name / version
