"""Train auditable PEFT adapters from CoreMesh golden datasets.

System role:
    Implements the Phase 4.3 offline customization boundary. One run reads a
    single reviewed feature scope, creates deterministic train/validation/test
    splits, trains a LoRA or QLoRA adapter, and emits immutable deployment and
    lineage artifacts. The held-out test split is never evaluated here.
Dependencies:
    Pydantic and SQLAlchemy provide configuration and PostgreSQL access. The
    isolated fine-tuner requirements add PyTorch, Transformers, PEFT, TRL,
    Datasets, bitsandbytes, safetensors, and optional W&B communication.
Side effects:
    Opens PostgreSQL connections, may download gated Hugging Face model files,
    allocates CPU/GPU memory, optionally sends metrics to W&B, and creates a
    new run directory. It never mutates PostgreSQL or overwrites an old run.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import importlib.metadata
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, text


DEFAULT_MODEL_ID = "meta-llama/Meta-Llama-3-8B-Instruct"
DEFAULT_MODEL_REVISION = "8afb486c1db24fe5011ec46dfbe5b5dccdb575c2"
MANIFEST_SCHEMA_VERSION = "coremesh-fine-tuning-manifest-v1"
CONFIG_SCHEMA_VERSION = "coremesh-fine-tuning-config-v1"
REQUIRED_ML_PACKAGES = (
    "accelerate",
    "bitsandbytes",
    "datasets",
    "peft",
    "safetensors",
    "torch",
    "transformers",
    "trl",
    "wandb",
)
_RUN_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class FineTunerError(RuntimeError):
    """Base class for user-correctable training failures."""


class FineTunerConfigurationError(FineTunerError):
    """Raised when configuration or credentials cannot start a safe run."""


class GoldenDatasetError(FineTunerError):
    """Raised when golden cases cannot form a valid training dataset."""


class FineTunerPreflightError(FineTunerError):
    """Raised when model or hardware prerequisites are unavailable."""


class FineTunerTrainingError(FineTunerError):
    """Raised when a run directory exists but training or export fails."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaseModelConfig(_StrictModel):
    """Pinned foundation-model identity for a training run."""

    model_id: str = DEFAULT_MODEL_ID
    revision: str = DEFAULT_MODEL_REVISION

    @field_validator("model_id", "revision")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class DatasetConfig(_StrictModel):
    """Golden-dataset selector and deterministic split policy."""

    feature_scope: str = Field(min_length=1, max_length=64)
    max_cases: int | None = Field(default=None, ge=3)
    seed: int = 42
    train_ratio: float = Field(default=0.80, gt=0.0, lt=1.0)
    validation_ratio: float = Field(default=0.10, gt=0.0, lt=1.0)
    test_ratio: float = Field(default=0.10, gt=0.0, lt=1.0)

    @field_validator("feature_scope")
    @classmethod
    def normalize_scope(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def ratios_total_one(self) -> "DatasetConfig":
        total = self.train_ratio + self.validation_ratio + self.test_ratio
        if abs(total - 1.0) > 1e-9:
            raise ValueError("train/validation/test ratios must total 1.0")
        return self


class LoraTrainingConfig(_StrictModel):
    """PEFT LoRA adapter shape."""

    rank: int = Field(default=16, ge=1)
    alpha: int = Field(default=32, ge=1)
    dropout: float = Field(default=0.05, ge=0.0, lt=1.0)
    target_modules: list[str] = Field(default_factory=lambda: ["q_proj", "v_proj"])

    @field_validator("target_modules")
    @classmethod
    def target_modules_are_unique(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if not normalized or any(not value for value in normalized):
            raise ValueError("target_modules must contain non-empty names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("target_modules must be unique")
        return normalized


class QuantizationConfig(_StrictModel):
    """bitsandbytes QLoRA settings."""

    enabled: bool = True
    quant_type: Literal["nf4", "fp4"] = "nf4"
    double_quant: bool = True


class TrainerConfig(_StrictModel):
    """TRL/Transformers optimization and checkpoint policy."""

    num_train_epochs: float = Field(default=3.0, gt=0.0)
    max_steps: int = Field(default=-1, ge=-1)
    learning_rate: float = Field(default=2e-4, gt=0.0)
    per_device_train_batch_size: int = Field(default=1, ge=1)
    per_device_eval_batch_size: int = Field(default=1, ge=1)
    gradient_accumulation_steps: int = Field(default=16, ge=1)
    max_length: int = Field(default=512, ge=16)
    logging_steps: int = Field(default=1, ge=1)
    gradient_checkpointing: bool = True
    early_stopping_patience: int = Field(default=2, ge=1)
    save_total_limit: int = Field(default=2, ge=1)

    @field_validator("max_steps")
    @classmethod
    def max_steps_is_minus_one_or_positive(cls, value: int) -> int:
        if value == 0:
            raise ValueError("max_steps must be -1 or a positive integer")
        return value


class WandbConfig(_StrictModel):
    """Weights & Biases experiment identity and network mode."""

    mode: Literal["online", "offline", "disabled"] = "online"
    project: str = "coremesh-fine-tuning"
    entity: str | None = None
    run_name: str | None = None
    tags: list[str] = Field(default_factory=lambda: ["coremesh", "qlora"])

    @field_validator("project")
    @classmethod
    def project_is_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("entity", "run_name")
    @classmethod
    def normalize_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class FineTuningConfig(_StrictModel):
    """Complete non-secret configuration required to reproduce a run."""

    schema_version: Literal[CONFIG_SCHEMA_VERSION] = CONFIG_SCHEMA_VERSION
    base_model: BaseModelConfig = Field(default_factory=BaseModelConfig)
    dataset: DatasetConfig
    lora: LoraTrainingConfig = Field(default_factory=LoraTrainingConfig)
    quantization: QuantizationConfig = Field(default_factory=QuantizationConfig)
    training: TrainerConfig = Field(default_factory=TrainerConfig)
    wandb: WandbConfig = Field(default_factory=WandbConfig)
    output_root: Path = Path("artifacts/fine_tuner")


@dataclass(frozen=True)
class GoldenTrainingCase:
    """One immutable source row used to construct an SFT example."""

    case_id: str
    feature_scope: str
    user_input: str
    expected_output: dict[str, Any]
    difficulty_rating: str
    origin_source: str
    source_fingerprint: str | None = None
    provenance: dict[str, Any] | None = None
    created_at: str | None = None

    @property
    def case_key(self) -> str:
        return self.source_fingerprint or self.case_id

    @property
    def reference_answer(self) -> str:
        raw = self.expected_output.get("reference_answer")
        return raw.strip() if isinstance(raw, str) else ""


@dataclass(frozen=True)
class DatasetSplits:
    """Deterministic, non-overlapping dataset partitions."""

    train: tuple[GoldenTrainingCase, ...]
    validation: tuple[GoldenTrainingCase, ...]
    test: tuple[GoldenTrainingCase, ...]

    @property
    def all_cases(self) -> tuple[GoldenTrainingCase, ...]:
        return (*self.train, *self.validation, *self.test)


@dataclass(frozen=True)
class RunResult:
    """Successful run identity returned to callers and the CLI."""

    run_id: str
    run_dir: Path
    manifest_path: Path
    adapter_dir: Path


@dataclass(frozen=True)
class _MLDependencies:
    torch: Any
    dataset_class: Any
    auto_config: Any
    auto_model: Any
    auto_tokenizer: Any
    bitsandbytes_config: Any
    early_stopping_callback: Any
    trainer_callback: Any
    lora_config: Any
    task_type: Any
    get_peft_model: Any
    prepare_model_for_kbit_training: Any
    sft_config: Any
    sft_trainer: Any


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically so interrupted runs cannot leave valid-looking files."""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_config(path: Path) -> FineTuningConfig:
    """Load strict JSON and resolve a relative output root beside the config."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FineTunerConfigurationError(f"cannot read config {path}: {error}") from error
    try:
        config = FineTuningConfig.model_validate(raw)
    except ValueError as error:
        raise FineTunerConfigurationError(f"invalid config {path}: {error}") from error
    if not config.output_root.is_absolute():
        config.output_root = (path.resolve().parent / config.output_root).resolve()
    return config


def _normalize_expected_output(raw: Any, *, case_id: str) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GoldenDatasetError(
                f"golden case {case_id} expected_output is invalid JSON"
            ) from error
    if not isinstance(raw, dict):
        raise GoldenDatasetError(
            f"golden case {case_id} expected_output must be a JSON object"
        )
    return raw


def load_golden_cases(postgres_dsn: str, feature_scope: str) -> list[GoldenTrainingCase]:
    """Read one feature scope without mutating the golden-dataset store."""

    engine = create_engine(postgres_dsn, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                        case_id::text AS case_id,
                        source_fingerprint,
                        feature_scope,
                        user_input,
                        expected_output,
                        difficulty_rating,
                        origin_source,
                        provenance,
                        created_at
                    FROM golden_datasets
                    WHERE feature_scope = :feature_scope
                    ORDER BY COALESCE(source_fingerprint, '') ASC, case_id ASC
                    """
                ),
                {"feature_scope": feature_scope},
            ).mappings()
            return [
                GoldenTrainingCase(
                    case_id=str(row["case_id"]),
                    source_fingerprint=(
                        str(row["source_fingerprint"]).strip()
                        if row["source_fingerprint"]
                        else None
                    ),
                    feature_scope=str(row["feature_scope"]),
                    user_input=str(row["user_input"]),
                    expected_output=_normalize_expected_output(
                        row["expected_output"], case_id=str(row["case_id"])
                    ),
                    difficulty_rating=str(row["difficulty_rating"]),
                    origin_source=str(row["origin_source"]),
                    provenance=(
                        dict(row["provenance"])
                        if isinstance(row["provenance"], Mapping)
                        else {}
                    ),
                    created_at=(
                        None
                        if row["created_at"] is None
                        else row["created_at"].isoformat()
                        if hasattr(row["created_at"], "isoformat")
                        else str(row["created_at"])
                    ),
                )
                for row in rows
            ]
    finally:
        engine.dispose()


def split_golden_cases(
    cases: Sequence[GoldenTrainingCase], config: DatasetConfig
) -> DatasetSplits:
    """Validate, deterministically limit, and split reviewed cases."""

    validated: list[GoldenTrainingCase] = []
    seen_keys: set[str] = set()
    seen_content: dict[str, str] = {}
    for case in cases:
        if case.feature_scope != config.feature_scope:
            raise GoldenDatasetError(
                f"case {case.case_key} belongs to {case.feature_scope!r}, "
                f"not {config.feature_scope!r}"
            )
        prompt = case.user_input.strip()
        completion = case.reference_answer
        if not prompt:
            raise GoldenDatasetError(f"case {case.case_key} has an empty user_input")
        if not completion:
            raise GoldenDatasetError(
                f"case {case.case_key} has no non-empty expected_output.reference_answer"
            )
        if case.case_key in seen_keys:
            raise GoldenDatasetError(f"duplicate golden case key {case.case_key}")
        seen_keys.add(case.case_key)
        content_hash = _sha256_json(
            {"prompt": " ".join(prompt.split()), "completion": " ".join(completion.split())}
        )
        if content_hash in seen_content:
            raise GoldenDatasetError(
                f"cases {seen_content[content_hash]} and {case.case_key} have duplicate content"
            )
        seen_content[content_hash] = case.case_key
        validated.append(case)

    if len(validated) < 3:
        raise GoldenDatasetError(
            f"feature scope {config.feature_scope!r} needs at least 3 valid cases; "
            f"found {len(validated)}"
        )

    shuffled = list(validated)
    random.Random(config.seed).shuffle(shuffled)
    if config.max_cases is not None:
        shuffled = shuffled[: config.max_cases]
    if len(shuffled) < 3:
        raise GoldenDatasetError("the configured case limit leaves fewer than 3 cases")

    validation_count = max(1, round(len(shuffled) * config.validation_ratio))
    test_count = max(1, round(len(shuffled) * config.test_ratio))
    if validation_count + test_count >= len(shuffled):
        validation_count = 1
        test_count = 1
    train_count = len(shuffled) - validation_count - test_count
    return DatasetSplits(
        train=tuple(shuffled[:train_count]),
        validation=tuple(shuffled[train_count : train_count + validation_count]),
        test=tuple(shuffled[train_count + validation_count :]),
    )


def _training_record(case: GoldenTrainingCase) -> dict[str, Any]:
    return {
        "prompt": [{"role": "user", "content": case.user_input.strip()}],
        "completion": [{"role": "assistant", "content": case.reference_answer}],
    }


def _case_lineage(case: GoldenTrainingCase) -> dict[str, Any]:
    row_payload = {
        "case_id": case.case_id,
        "source_fingerprint": case.source_fingerprint,
        "feature_scope": case.feature_scope,
        "user_input": case.user_input,
        "expected_output": case.expected_output,
        "difficulty_rating": case.difficulty_rating,
        "origin_source": case.origin_source,
        "provenance": case.provenance or {},
        "created_at": case.created_at,
    }
    return {
        "case_id": case.case_id,
        "case_key": case.case_key,
        "source_fingerprint": case.source_fingerprint,
        "difficulty_rating": case.difficulty_rating,
        "origin_source": case.origin_source,
        "provenance": case.provenance or {},
        "created_at": case.created_at,
        "row_sha256": _sha256_json(row_payload),
    }


def dataset_lineage(splits: DatasetSplits) -> dict[str, Any]:
    """Return redacted split lineage and a digest over exact source rows."""

    split_payload = {
        "train": [_case_lineage(case) for case in splits.train],
        "validation": [_case_lineage(case) for case in splits.validation],
        "test": [_case_lineage(case) for case in splits.test],
    }
    return {
        "counts": {name: len(values) for name, values in split_payload.items()},
        "splits": split_payload,
        "dataset_sha256": _sha256_json(split_payload),
    }


def resolved_config_payload(config: FineTuningConfig) -> dict[str, Any]:
    """Serialize only non-secret configuration for artifacts and W&B."""

    return config.model_dump(mode="json")


def wandb_config_payload(
    config: FineTuningConfig, lineage: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the bounded W&B config without prompts, DSNs, or credentials."""

    payload = resolved_config_payload(config)
    payload["dataset_summary"] = {
        "counts": dict(lineage["counts"]),
        "dataset_sha256": lineage["dataset_sha256"],
    }
    return payload


def _import_ml_dependencies() -> _MLDependencies:
    """Load the heavyweight training stack only for an actual training run."""

    try:
        torch = importlib.import_module("torch")
        datasets_module = importlib.import_module("datasets")
        transformers = importlib.import_module("transformers")
        peft = importlib.import_module("peft")
        trl = importlib.import_module("trl")
    except ImportError as error:
        missing = getattr(error, "name", None) or str(error)
        raise FineTunerPreflightError(
            f"missing fine-tuner dependency {missing!r}; "
            "install requirements-fine-tuner.txt"
        ) from error
    return _MLDependencies(
        torch=torch,
        dataset_class=datasets_module.Dataset,
        auto_config=transformers.AutoConfig,
        auto_model=transformers.AutoModelForCausalLM,
        auto_tokenizer=transformers.AutoTokenizer,
        bitsandbytes_config=transformers.BitsAndBytesConfig,
        early_stopping_callback=transformers.EarlyStoppingCallback,
        trainer_callback=transformers.TrainerCallback,
        lora_config=peft.LoraConfig,
        task_type=peft.TaskType,
        get_peft_model=peft.get_peft_model,
        prepare_model_for_kbit_training=peft.prepare_model_for_kbit_training,
        sft_config=trl.SFTConfig,
        sft_trainer=trl.SFTTrainer,
    )


def _model_kwargs(
    config: FineTuningConfig, environ: Mapping[str, str]
) -> dict[str, Any]:
    model_path = Path(config.base_model.model_id)
    if model_path.exists():
        return {"local_files_only": True}
    token = environ.get("HF_TOKEN", "").strip()
    return {
        "revision": config.base_model.revision,
        "token": token or None,
        "trust_remote_code": False,
    }


def build_quantization_config(config: QuantizationConfig, deps: _MLDependencies) -> Any:
    """Build the NF4/FP4 Transformers configuration used by QLoRA."""

    compute_dtype = (
        deps.torch.bfloat16
        if deps.torch.cuda.is_available() and deps.torch.cuda.is_bf16_supported()
        else deps.torch.float16
    )
    return deps.bitsandbytes_config(
        load_in_4bit=True,
        bnb_4bit_quant_type=config.quant_type,
        bnb_4bit_use_double_quant=config.double_quant,
        bnb_4bit_compute_dtype=compute_dtype,
    )


def _preflight(
    config: FineTuningConfig,
    deps: _MLDependencies,
    environ: Mapping[str, str],
) -> str:
    if config.quantization.enabled:
        if not deps.torch.cuda.is_available():
            raise FineTunerPreflightError(
                "QLoRA requires an available CUDA GPU; set quantization.enabled=false "
                "only for CPU debugging"
            )
        try:
            importlib.import_module("bitsandbytes")
        except ImportError as error:
            raise FineTunerPreflightError(
                "QLoRA requires bitsandbytes from requirements-fine-tuner.txt"
            ) from error
    if config.wandb.mode == "online" and not environ.get("WANDB_API_KEY", "").strip():
        raise FineTunerConfigurationError(
            "WANDB_API_KEY is required when wandb.mode is 'online'"
        )
    if (
        config.base_model.model_id.startswith("meta-llama/")
        and not environ.get("HF_TOKEN", "").strip()
    ):
        raise FineTunerConfigurationError(
            "HF_TOKEN is required for the gated Meta Llama model; accept its license first"
        )
    try:
        model_config = deps.auto_config.from_pretrained(
            config.base_model.model_id,
            **_model_kwargs(config, environ),
        )
    except Exception as error:
        raise FineTunerPreflightError(
            f"cannot access base model {config.base_model.model_id!r} at "
            f"revision {config.base_model.revision!r}: {error}"
        ) from error
    if Path(config.base_model.model_id).exists():
        return _hash_local_model_identity(Path(config.base_model.model_id))
    return str(getattr(model_config, "_commit_hash", None) or config.base_model.revision)


def _hash_local_model_identity(path: Path) -> str:
    entries = []
    for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
        entries.append(
            {
                "path": candidate.relative_to(path).as_posix(),
                "size": candidate.stat().st_size,
                "sha256": _sha256_file(candidate),
            }
        )
    return _sha256_json(entries)


def _run_component(value: str) -> str:
    normalized = _RUN_COMPONENT_PATTERN.sub("-", value.strip()).strip("-.")
    return normalized[:80] or "run"


def _new_run_id(config_hash: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{config_hash[:10]}"


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in REQUIRED_ML_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _git_metadata() -> dict[str, Any]:
    def run(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    revision = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"revision": revision, "dirty": bool(status) if status is not None else None}


def _hardware_metadata(torch: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "torch_version": str(torch.__version__),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_runtime": getattr(torch.version, "cuda", None),
    }
    if torch.cuda.is_available():
        device = torch.cuda.current_device()
        properties = torch.cuda.get_device_properties(device)
        payload.update(
            {
                "cuda_device": device,
                "gpu_name": torch.cuda.get_device_name(device),
                "gpu_capability": list(torch.cuda.get_device_capability(device)),
                "gpu_total_memory_bytes": int(properties.total_memory),
            }
        )
    return payload


def _cuda_memory(torch: Any) -> dict[str, int]:
    if not torch.cuda.is_available():
        return {
            "allocated_bytes": 0,
            "reserved_bytes": 0,
            "peak_allocated_bytes": 0,
            "peak_reserved_bytes": 0,
        }
    device = torch.cuda.current_device()
    return {
        "allocated_bytes": int(torch.cuda.memory_allocated(device)),
        "reserved_bytes": int(torch.cuda.memory_reserved(device)),
        "peak_allocated_bytes": int(torch.cuda.max_memory_allocated(device)),
        "peak_reserved_bytes": int(torch.cuda.max_memory_reserved(device)),
    }


def _build_memory_callback(deps: _MLDependencies, started: float) -> Any:
    class MemoryTelemetryCallback(deps.trainer_callback):
        """Add elapsed-time and CUDA allocator metrics to Trainer/W&B logs."""

        def on_log(self, args: Any, state: Any, control: Any, logs: Any = None, **_: Any) -> Any:
            metrics: dict[str, Any] = {
                "runtime/elapsed_seconds": round(time.monotonic() - started, 6)
            }
            metrics.update(
                {f"gpu/{key}": value for key, value in _cuda_memory(deps.torch).items()}
            )
            try:
                wandb = importlib.import_module("wandb")
            except ImportError:
                return control
            if wandb.run is not None:
                wandb.run.log(metrics, step=state.global_step)
            return control

    return MemoryTelemetryCallback()


def _configure_wandb_for_trainer(
    config: FineTuningConfig,
    run_id: str,
) -> bool:
    """Apply non-secret W&B settings before the Trainer initializes the run."""

    if config.wandb.mode == "disabled":
        return False
    try:
        importlib.import_module("wandb")
    except ImportError as error:
        raise FineTunerPreflightError(
            "Weights & Biases logging requires wandb from requirements-fine-tuner.txt"
        ) from error
    os.environ["WANDB_PROJECT"] = config.wandb.project
    os.environ["WANDB_MODE"] = config.wandb.mode
    os.environ["WANDB_NAME"] = config.wandb.run_name or run_id
    if config.wandb.entity:
        os.environ["WANDB_ENTITY"] = config.wandb.entity
    else:
        os.environ.pop("WANDB_ENTITY", None)
    return True


def _build_wandb_config_callback(
    deps: _MLDependencies,
    config: FineTuningConfig,
    lineage: Mapping[str, Any],
) -> Any:
    class WandbConfigCallback(deps.trainer_callback):
        """Publish resolved hyperparameters once the Trainer opens the W&B run."""

        def on_train_begin(self, args: Any, state: Any, control: Any, **_: Any) -> Any:
            try:
                wandb = importlib.import_module("wandb")
            except ImportError:
                return control
            if wandb.run is None:
                return control
            if config.wandb.tags:
                wandb.run.tags = list(config.wandb.tags)
            wandb.config.update(wandb_config_payload(config, lineage), allow_val_change=True)
            return control

    return WandbConfigCallback()


def _active_wandb_run() -> Any:
    try:
        wandb = importlib.import_module("wandb")
    except ImportError:
        return None
    return wandb.run


def _finish_wandb_run() -> None:
    run = _active_wandb_run()
    if run is not None:
        try:
            run.finish()
        except Exception:
            pass


def _artifact_inventory(run_dir: Path, adapter_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    candidates = [run_dir / "resolved_config.json"]
    candidates.extend(sorted(path for path in adapter_dir.rglob("*") if path.is_file()))
    for path in candidates:
        artifacts.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return artifacts


def _training_metrics(trainer: Any, train_result: Any) -> dict[str, Any]:
    metrics = dict(getattr(train_result, "metrics", {}) or {})
    evaluations = [
        dict(entry)
        for entry in getattr(trainer.state, "log_history", [])
        if "eval_loss" in entry
    ]
    if evaluations:
        metrics["final_evaluation"] = evaluations[-1]
    metrics["best_metric"] = getattr(trainer.state, "best_metric", None)
    best_checkpoint = getattr(trainer.state, "best_model_checkpoint", None)
    metrics["best_checkpoint"] = Path(best_checkpoint).name if best_checkpoint else None
    metrics["global_step"] = int(getattr(trainer.state, "global_step", 0))
    return metrics


def _safe_error_message(error: BaseException, environ: Mapping[str, str]) -> str:
    message = str(error)
    for name in ("POSTGRES_DSN", "HF_TOKEN", "WANDB_API_KEY"):
        secret = environ.get(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:2000]


def _cleanup_training_objects(torch: Any) -> None:
    """Drop large references and release unoccupied CUDA blocks after every run."""

    gc.collect()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        gc.collect()


def run_training(
    config: FineTuningConfig,
    *,
    cases: Sequence[GoldenTrainingCase] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RunResult:
    """Execute one isolated LoRA/QLoRA run and return its final artifact paths.

    ``cases`` is injectable for offline tests. Production callers omit it and
    provide ``POSTGRES_DSN`` through the environment.
    """

    runtime_env = dict(os.environ if environ is None else environ)
    if cases is None:
        postgres_dsn = runtime_env.get("POSTGRES_DSN", "").strip()
        if not postgres_dsn:
            raise FineTunerConfigurationError("POSTGRES_DSN is required")
        try:
            cases = load_golden_cases(postgres_dsn, config.dataset.feature_scope)
        except GoldenDatasetError:
            raise
        except Exception as error:
            raise FineTunerPreflightError(
                "cannot read golden_datasets: " + _safe_error_message(error, runtime_env)
            ) from error
    splits = split_golden_cases(cases, config.dataset)
    lineage = dataset_lineage(splits)
    resolved_config = resolved_config_payload(config)
    config_hash = _sha256_json(resolved_config)

    deps = _import_ml_dependencies()
    resolved_model_revision = _preflight(config, deps, runtime_env)

    scope_dir = config.output_root / _run_component(config.dataset.feature_scope)
    run_id = _new_run_id(config_hash)
    run_dir = scope_dir / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise FineTunerConfigurationError(
            f"refusing to overwrite existing run directory {run_dir}"
        ) from error
    except OSError as error:
        raise FineTunerConfigurationError(
            f"cannot create run directory {run_dir}: {error}"
        ) from error
    checkpoints_dir = run_dir / "checkpoints"
    adapter_dir = run_dir / "adapter"
    checkpoints_dir.mkdir()
    _atomic_write_json(run_dir / "resolved_config.json", resolved_config)

    trainer = None
    model = None
    tokenizer = None
    export_model = None
    wandb_enabled = _configure_wandb_for_trainer(config, run_id)
    started = time.monotonic()
    started_at = utc_now()
    if deps.torch.cuda.is_available():
        deps.torch.cuda.reset_peak_memory_stats()

    try:
        source_kwargs = _model_kwargs(config, runtime_env)
        tokenizer = deps.auto_tokenizer.from_pretrained(
            config.base_model.model_id,
            **source_kwargs,
        )
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                raise FineTunerPreflightError(
                    "the base tokenizer defines neither a pad token nor an EOS token"
                )
            tokenizer.pad_token = tokenizer.eos_token
        tokenizer.padding_side = "right"

        model_kwargs: dict[str, Any] = {**source_kwargs, "low_cpu_mem_usage": True}
        if config.quantization.enabled:
            quantization_config = build_quantization_config(config.quantization, deps)
            model_kwargs.update(
                {
                    "quantization_config": quantization_config,
                    "device_map": {"": deps.torch.cuda.current_device()},
                    "dtype": quantization_config.bnb_4bit_compute_dtype,
                }
            )
        else:
            model_kwargs["dtype"] = deps.torch.float32
        model = deps.auto_model.from_pretrained(config.base_model.model_id, **model_kwargs)
        model.config.use_cache = False
        if config.quantization.enabled:
            model = deps.prepare_model_for_kbit_training(
                model,
                use_gradient_checkpointing=config.training.gradient_checkpointing,
            )
        elif config.training.gradient_checkpointing:
            model.gradient_checkpointing_enable()
            model.enable_input_require_grads()

        peft_config = deps.lora_config(
            r=config.lora.rank,
            lora_alpha=config.lora.alpha,
            lora_dropout=config.lora.dropout,
            target_modules=config.lora.target_modules,
            bias="none",
            task_type=deps.task_type.CAUSAL_LM,
        )
        model = deps.get_peft_model(model, peft_config)
        trainable_names = [name for name, value in model.named_parameters() if value.requires_grad]
        if not trainable_names:
            raise FineTunerPreflightError("PEFT created no trainable adapter parameters")
        unexpected = [name for name in trainable_names if "lora_" not in name]
        if unexpected:
            raise FineTunerPreflightError(
                "non-adapter parameters unexpectedly remain trainable: "
                + ", ".join(unexpected[:5])
            )

        train_dataset = deps.dataset_class.from_list(
            [_training_record(case) for case in splits.train]
        )
        validation_dataset = deps.dataset_class.from_list(
            [_training_record(case) for case in splits.validation]
        )
        use_bf16 = bool(
            config.quantization.enabled
            and deps.torch.cuda.is_available()
            and deps.torch.cuda.is_bf16_supported()
        )
        training_args = deps.sft_config(
            output_dir=str(checkpoints_dir),
            num_train_epochs=config.training.num_train_epochs,
            max_steps=config.training.max_steps,
            learning_rate=config.training.learning_rate,
            per_device_train_batch_size=config.training.per_device_train_batch_size,
            per_device_eval_batch_size=config.training.per_device_eval_batch_size,
            gradient_accumulation_steps=config.training.gradient_accumulation_steps,
            max_length=config.training.max_length,
            completion_only_loss=True,
            packing=False,
            logging_strategy="steps",
            logging_steps=config.training.logging_steps,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=config.training.save_total_limit,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            gradient_checkpointing=config.training.gradient_checkpointing,
            bf16=use_bf16,
            fp16=bool(config.quantization.enabled and not use_bf16),
            optim="paged_adamw_8bit" if config.quantization.enabled else "adamw_torch",
            report_to=["wandb"] if wandb_enabled else "none",
            run_name=config.wandb.run_name or run_id,
            seed=config.dataset.seed,
            data_seed=config.dataset.seed,
            dataloader_pin_memory=bool(deps.torch.cuda.is_available()),
        )
        callbacks = [
            deps.early_stopping_callback(
                early_stopping_patience=config.training.early_stopping_patience
            ),
            _build_memory_callback(deps, started),
        ]
        if wandb_enabled:
            callbacks.append(_build_wandb_config_callback(deps, config, lineage))
        trainer = deps.sft_trainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=validation_dataset,
            processing_class=tokenizer,
            callbacks=callbacks,
        )
        train_result = trainer.train()
        metrics = _training_metrics(trainer, train_result)

        adapter_dir.mkdir()
        export_model = trainer.accelerator.unwrap_model(trainer.model)
        export_model.save_pretrained(adapter_dir, safe_serialization=True)
        tokenizer.save_pretrained(adapter_dir)
        adapter_weights = adapter_dir / "adapter_model.safetensors"
        if not adapter_weights.is_file():
            raise RuntimeError("PEFT did not emit adapter_model.safetensors")

        finished_at = utc_now()
        elapsed = round(time.monotonic() - started, 6)
        memory = _cuda_memory(deps.torch)
        wandb_identity = None
        wandb_run = _active_wandb_run()
        if wandb_run is not None:
            wandb_identity = {
                "id": getattr(wandb_run, "id", None),
                "name": getattr(wandb_run, "name", None),
                "url": getattr(wandb_run, "url", None),
                "mode": config.wandb.mode,
            }
            wandb_run.summary.update(
                {
                    "best_checkpoint": metrics["best_checkpoint"],
                    "best_metric": metrics["best_metric"],
                    "training_duration_seconds": elapsed,
                    **{f"gpu_{key}": value for key, value in memory.items()},
                }
            )
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "completed",
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": elapsed,
            "feature_scope": config.dataset.feature_scope,
            "base_model": {
                "model_id": config.base_model.model_id,
                "requested_revision": config.base_model.revision,
                "resolved_revision": resolved_model_revision,
            },
            "config_sha256": config_hash,
            "dataset": lineage,
            "metrics": metrics,
            "memory": memory,
            "trainable_parameters": {
                "count": sum(
                    int(parameter.numel())
                    for parameter in model.parameters()
                    if parameter.requires_grad
                ),
                "names": trainable_names,
            },
            "packages": _package_versions(),
            "hardware": _hardware_metadata(deps.torch),
            "git": _git_metadata(),
            "wandb": wandb_identity,
            "artifacts": _artifact_inventory(run_dir, adapter_dir),
        }
        manifest_path = run_dir / "manifest.json"
        _atomic_write_json(manifest_path, manifest)
        return RunResult(
            run_id=run_id,
            run_dir=run_dir,
            manifest_path=manifest_path,
            adapter_dir=adapter_dir,
        )
    except Exception as error:
        # A manifest is the promotion boundary. If finalization fails after
        # serialization, remove the adapter while retaining checkpoints and
        # the failure report for diagnosis.
        if adapter_dir.exists():
            shutil.rmtree(adapter_dir, ignore_errors=True)
        failure_manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "failed",
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_seconds": round(time.monotonic() - started, 6),
            "feature_scope": config.dataset.feature_scope,
            "base_model": {
                "model_id": config.base_model.model_id,
                "requested_revision": config.base_model.revision,
                "resolved_revision": resolved_model_revision,
            },
            "config_sha256": config_hash,
            "dataset": lineage,
            "error": {
                "type": type(error).__name__,
                "message": _safe_error_message(error, runtime_env),
            },
            "memory": _cuda_memory(deps.torch),
            "packages": _package_versions(),
            "hardware": _hardware_metadata(deps.torch),
            "git": _git_metadata(),
        }
        _atomic_write_json(run_dir / "manifest.failed.json", failure_manifest)
        if isinstance(error, FineTunerTrainingError):
            raise
        if isinstance(error, FineTunerError):
            raise FineTunerTrainingError(str(error)) from error
        raise
    finally:
        _finish_wandb_run()
        export_model = None
        trainer = None
        model = None
        tokenizer = None
        _cleanup_training_objects(deps.torch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train a CoreMesh PEFT/QLoRA adapter from golden_datasets"
    )
    parser.add_argument("--config", type=Path, required=True, help="strict JSON config path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_training(load_config(args.config))
    except FineTunerTrainingError as error:
        print(f"fine-tuner: {error}", file=sys.stderr)
        return 1
    except FineTunerError as error:
        print(f"fine-tuner: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"fine-tuner: unexpected training failure: {error}", file=sys.stderr)
        return 1
    print(result.manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
