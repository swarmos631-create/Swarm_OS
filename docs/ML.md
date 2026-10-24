# Local AI and the model pipeline

SwarmOS's core AI runs entirely on the host. There are **no** OpenAI, Anthropic, Gemini or other hosted LLM
adapters in the codebase, and a test (`test_no_hosted_llm_provider_code_exists`) fails if one is added.

## Runtime (`backend/swarmos/ai/runtime.py`)

| Provider | Used for | Availability |
|---|---|---|
| `sklearn` | Trained artifacts from this pipeline (classifier, forecasters, anomaly detector) | After `swarmos ml train … --promote-if-better` |
| `ollama` | Optional prose for summarizer agents; embeddings | `SWARMOS_OLLAMA_URL` (loopback only) + `SWARMOS_LLM_NARRATIVES_ENABLED=true` |
| `llamacpp` | Same, via `llama-server` | `SWARMOS_LLAMACPP_URL` (loopback only) |
| `rules` | Deterministic templates and rule baselines | Always |

* Endpoints that are not on loopback are rejected at startup unless `SWARMOS_ALLOW_REMOTE_MODEL_HOSTS=true` is set
  for a self-hosted server you operate.
* LLM calls have a timeout and a concurrency limit, and their structured output is validated (`generate_structured`
  checks the result against a Pydantic schema; invalid output is discarded and the deterministic path is used).
* LLMs only write **prose** over facts that are already structured. Findings, metrics and actions never come from an LLM.
* A trained artifact loads only when its status is `promoted` **and** its SHA-256 matches the registry.

### Optional: enable a local LLM

```bash
brew install ollama && ollama serve        # or see https://ollama.com
ollama pull llama3.2:3b                     # ~2 GB — check disk and the model's licence first
echo 'SWARMOS_OLLAMA_URL=http://127.0.0.1:11434' >> .env
echo 'SWARMOS_LLM_NARRATIVES_ENABLED=true' >> .env
```

The Model Lab page shows whether each provider is reachable and whether the configured model is pulled.

## Pipeline (`backend/swarmos/ml/`)

```
backend/swarmos/ml/configs/<model>.yaml   reproducible training config (dataset generator + params, split,
                                          hyperparameters, gate); package data, override with SWARMOS_ML_CONFIGS_DIR
ml/                                       scratch root (/tmp/swarmos-ml on Vercel)
├── datasets/synthetic/<name>/    data.csv + manifest.json (sha256, rows, licence, provenance, generator params)
├── datasets/raw|processed/       place authorised real datasets here (git-ignored)
├── artifacts/<model>/<version>/  model.joblib + metadata.json (git-ignored; local copy)
└── reports/<model>/<version>.md  evaluation report (git-ignored; local copy)
```

The registry row is the source of truth: `model_versions.artifact_blob` holds the joblib bytes and
`model_versions.report_md` the report. Loading verifies the blob's SHA-256 against the registry, so any API instance
(including serverless ones without a shared disk) can serve a promoted model. Training jobs are regular
`train_model` jobs and follow `SWARMOS_JOB_MODE` (see [ARCHITECTURE.md](ARCHITECTURE.md#background-work)).

Steps in `pipeline.train_model`:

1. Load the config and ensure the dataset exists. It is regenerated deterministically when the generator params
   change, and its checksum is verified when loaded.
2. **Validate** (`datasets.validate`): schema, types, ranges, nulls, allowed labels, licence and provenance present,
   and a privacy scan for Indian mobile numbers, emails, Aadhaar-like and PAN-like patterns. Any hit fails the run.
3. **Split**: template-holdout for text (an unseen phrasing family per class goes to the test set), time-based for
   forecasting (last 14 days test, previous 7 days validation), time-ordered for anomaly detection.
4. **Train** with fixed seeds on CPU.
5. **Evaluate against a baseline**: keyword rules (classifier), seasonal-naive 168 h (forecasters), engineering
   bands (anomaly detector).
6. **Gate**: the configured metric must beat the baseline by the margin. Versions that fail the gate are registered
   but cannot be promoted without an audited admin *force*.
7. Write the artifact, metadata (environment, checksums, split sizes) and a Markdown report; register a
   `model_versions` row that stores the artifact bytes and the report.
8. Optionally promote. `promote` re-verifies the artifact checksum and archives the previous version. `rollback`
   re-promotes the previously promoted version.

### Models

| Model | Task | Algorithm | Agent |
|---|---|---|---|
| `complaint_classifier` | 12-class complaint text | TF-IDF word + char n-grams + keyword lexicon → logistic regression | Complaint Classification; service-request intake |
| `energy_demand_forecaster` | 24 h-ahead hourly MW | HistGradientBoosting on lags ≥ 24 h + calendar | Energy Demand Forecaster |
| `water_demand_forecaster` | 24 h-ahead hourly m³/h | same | Water Demand Analyst |
| `congestion_forecaster` | 24 h-ahead city-mean speed | same | Congestion Forecast Agent |
| `sensor_anomaly_detector` | grid telemetry outliers | Isolation Forest | Grid Anomaly Agent |

Feature engineering lives in `ml/features.py` and is shared by training and inference, so the two cannot drift apart.

### Limitations (read before relying on any metric)

* Every bundled dataset is **synthetic**, generated from templates and parametric curves. Metrics show that the
  pipeline works; they say nothing about real-world accuracy. In particular, the complaint classifier's test-set
  score is not independent of its features, because the keyword lexicon and the templates were written by the
  same author.
* Retrain on authorised, representative city data (for example, labelled grievances, SLDC demand, SCADA telemetry)
  placed in `ml/datasets/raw`, with a config pointing at a real ingestion function, before operational use.
* GPU acceleration is not needed for these models. scikit-learn runs on CPU, and any model added later can use a GPU
  through its own trainer.

### Commands

```bash
backend/.venv/bin/swarmos ml list
backend/.venv/bin/swarmos ml train energy_demand_forecaster --promote-if-better
backend/.venv/bin/swarmos ml train-all --promote-if-better
backend/.venv/bin/swarmos ml promote complaint_classifier <version> [--force]
backend/.venv/bin/swarmos ml rollback complaint_classifier
```

## Retrieval (`ai/retrieval.py`)

Local BM25 over `document_chunks`, filtered by the caller's clearance (viewer → public; operator → + internal;
admin → + restricted) and by city scope. Results carry title, source, licence, classification, and
`content_role: retrieved_data_not_instructions`. No component treats retrieved text as instructions.
Operators can ingest documents through `POST /cities/{id}/documents`.
