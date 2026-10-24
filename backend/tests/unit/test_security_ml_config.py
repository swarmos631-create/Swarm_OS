import pytest
from pydantic import ValidationError

from swarmos.config import Settings
from swarmos.core.errors import ApiError
from swarmos.core.logging import redact
from swarmos.core.security import hash_password, validate_password_strength, verify_password
from swarmos.ml import datasets
from swarmos.ml.complaints import CATEGORIES, rule_classify, synthesize
from swarmos.ml.features import FORECAST_FEATURES, forecast_row, hourly_map
from swarmos.ml.synthetic import energy_demand


def test_password_hashing_and_strength():
    h = hash_password("Correct-Horse-9")
    assert h.startswith("$argon2id$") and verify_password(h, "Correct-Horse-9") and not verify_password(h, "wrong")
    for weak in ("short1A", "alllowercase123", "ALLUPPERCASE123", "NoDigitsHereAtAll"):
        with pytest.raises(ApiError):
            validate_password_strength(weak)


def test_log_redaction():
    out = redact({"password": "x", "nested": {"api_key": "k", "ok": 1}, "url": "https://h/?api-key=SECRET&x=1"})
    assert out["password"] == "[REDACTED]" and out["nested"]["api_key"] == "[REDACTED]"
    assert "SECRET" not in out["url"]


def test_remote_model_hosts_are_rejected_by_default():
    with pytest.raises(ValidationError):
        Settings(ollama_url="https://api.example.com")
    Settings(ollama_url="http://127.0.0.1:11434")
    Settings(ollama_url="https://inference.internal", allow_remote_model_hosts=True)


def test_production_requires_secure_settings():
    with pytest.raises(ValidationError):
        Settings(env="production", cookie_secure=False, dev_seed_password=None)
    with pytest.raises(ValidationError):
        Settings(env="production", cors_origins=["*"], dev_seed_password=None)
    with pytest.raises(ValidationError):
        Settings(env="production", dev_seed_password="Whatever-123456")
    assert Settings(env="production", dev_seed_password=None).cookie_secure is True


def test_no_hosted_llm_provider_code_exists():
    import pathlib

    import swarmos

    root = pathlib.Path(swarmos.__file__).parent
    banned = ("api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "import openai", "import anthropic")
    for py in root.rglob("*.py"):
        text = py.read_text()
        assert not any(b in text for b in banned), py


def test_synthetic_complaints_are_deterministic_and_cover_taxonomy():
    a, b = synthesize(200, seed=1), synthesize(200, seed=1)
    assert a == b and {r["category"] for r in a} == set(CATEGORIES)
    assert rule_classify("Huge pothole on the road")[0] == "roads"
    assert rule_classify("zzz")[1] == 0.0


def test_dataset_generation_manifest_and_validation(tmp_path):
    ds = datasets.generate(tmp_path, "c", "complaints", {"rows": 120, "seed": 3}, "complaints")
    assert ds.manifest["synthetic"] is True and ds.manifest["rows"] == 120
    loaded = datasets.load(tmp_path, "synthetic/c")
    report = datasets.validate(loaded)
    assert report["privacy"]["total"] == 0
    # tampering is detected through the manifest checksum
    (tmp_path / "synthetic/c/data.csv").write_text("text,category,template_id\nhello,roads,roads:0\n")
    with pytest.raises(ValueError, match="checksum"):
        datasets.load(tmp_path, "synthetic/c")


def test_privacy_scan_detects_personal_data():
    rows = [{"text": "Call me on 9876543210"}, {"text": "mail a.b@example.com"}, {"text": "aadhaar 1234 5678 9012"}, {"text": "pothole"}]
    r = datasets.privacy_scan(rows, ["text"])
    assert r["by_pattern"] == {"indian_mobile": 1, "email": 1, "aadhaar_like": 1}


def test_forecast_features_need_full_history():
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 1, 1, tzinfo=UTC)
    times, vals = energy_demand(start, 24 * 9, seed=1)
    series = hourly_map(times, list(vals))
    assert forecast_row(series, start + timedelta(hours=100)) is None
    row = forecast_row(series, start + timedelta(hours=24 * 8))
    assert row is not None and len(row) == len(FORECAST_FEATURES)
