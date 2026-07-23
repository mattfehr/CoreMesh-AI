"""Unit and opt-in integration tests for the Phase 4.3 fine-tuner.

System role:
    Protects deterministic golden-data preparation, secret-free lineage,
    adapter export, and CUDA cleanup. The normal suite stays offline; heavy ML
    tests skip unless the isolated fine-tuner dependencies are installed.
Dependencies:
    Pytest and the analytics base dependencies cover unit tests. CPU/GPU
    pipeline tests additionally use requirements-fine-tuner.txt.
Side effects:
    Integration tests create only temporary local models and run artifacts.
    The opt-in GPU test allocates CUDA memory but performs no network calls.
"""
from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fine_tuner import train


def _case(index: int, *, scope: str = "support", answer: str | None = None):
    return train.GoldenTrainingCase(
        case_id=f"case-{index}",
        source_fingerprint=f"{index:064x}",
        feature_scope=scope,
        user_input=f"Resolve support request number {index} safely.",
        expected_output={
            "reference_answer": answer or f"Resolution number {index} is complete.",
            "validation_criteria": [{"description": "Safe resolution", "required": True}],
            "expected_behavior": "answer",
            "failure_pattern": "Incomplete resolution",
        },
        difficulty_rating="moderate",
        origin_source="production_miner",
        provenance={"candidate_id": f"candidate-{index}", "license": "internal"},
        created_at="2026-07-22T00:00:00Z",
    )


def _config(
    tmp_path: Path,
    *,
    quantized: bool = False,
    model_id: str = "local-model",
) -> train.FineTuningConfig:
    return train.FineTuningConfig(
        base_model={"model_id": model_id, "revision": "local"},
        dataset={"feature_scope": "support", "seed": 19},
        quantization={"enabled": quantized},
        training={
            "num_train_epochs": 1,
            "max_steps": -1,
            "learning_rate": 1e-3,
            "per_device_train_batch_size": 4,
            "per_device_eval_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "max_length": 64,
            "logging_steps": 1,
            "gradient_checkpointing": False,
            "early_stopping_patience": 1,
            "save_total_limit": 1,
        },
        wandb={"mode": "disabled", "project": "test"},
        output_root=tmp_path / "runs",
    )


def test_split_is_deterministic_disjoint_and_eighty_ten_ten() -> None:
    cases = [_case(index) for index in range(10)]
    config = train.DatasetConfig(feature_scope="support", seed=7)

    first = train.split_golden_cases(cases, config)
    second = train.split_golden_cases(list(reversed(cases)), config)

    # Database order is part of the stable input contract, so compare repeated
    # runs with the same source order and separately verify split invariants.
    repeated = train.split_golden_cases(cases, config)
    assert [case.case_key for case in first.all_cases] == [
        case.case_key for case in repeated.all_cases
    ]
    assert (len(first.train), len(first.validation), len(first.test)) == (8, 1, 1)
    assert set(case.case_key for case in first.all_cases) == set(
        case.case_key for case in second.all_cases
    )
    split_keys = [
        {case.case_key for case in first.train},
        {case.case_key for case in first.validation},
        {case.case_key for case in first.test},
    ]
    assert split_keys[0].isdisjoint(split_keys[1])
    assert split_keys[0].isdisjoint(split_keys[2])
    assert split_keys[1].isdisjoint(split_keys[2])


def test_split_applies_deterministic_small_case_limit() -> None:
    cases = [_case(index) for index in range(20)]
    config = train.DatasetConfig(feature_scope="support", max_cases=6, seed=11)

    splits = train.split_golden_cases(cases, config)

    assert (len(splits.train), len(splits.validation), len(splits.test)) == (4, 1, 1)
    assert [case.case_key for case in splits.all_cases] == [
        case.case_key
        for case in train.split_golden_cases(cases, config).all_cases
    ]


def test_split_rejects_wrong_scope_missing_target_and_duplicate_content() -> None:
    with pytest.raises(train.GoldenDatasetError, match="belongs to"):
        train.split_golden_cases(
            [_case(1), _case(2), _case(3, scope="other")],
            train.DatasetConfig(feature_scope="support"),
        )

    missing = _case(2)
    missing = train.GoldenTrainingCase(
        **{**missing.__dict__, "expected_output": {"reference_answer": " "}}
    )
    with pytest.raises(train.GoldenDatasetError, match="reference_answer"):
        train.split_golden_cases(
            [_case(1), missing, _case(3)],
            train.DatasetConfig(feature_scope="support"),
        )

    duplicate = train.GoldenTrainingCase(
        **{
            **_case(4).__dict__,
            "user_input": _case(1).user_input,
            "expected_output": _case(1).expected_output,
        }
    )
    with pytest.raises(train.GoldenDatasetError, match="duplicate content"):
        train.split_golden_cases(
            [_case(1), _case(2), duplicate],
            train.DatasetConfig(feature_scope="support"),
        )


def test_lineage_has_row_hashes_and_no_training_text() -> None:
    splits = train.split_golden_cases(
        [_case(index) for index in range(6)],
        train.DatasetConfig(feature_scope="support"),
    )

    lineage = train.dataset_lineage(splits)
    serialized = json.dumps(lineage)

    assert lineage["counts"] == {"train": 4, "validation": 1, "test": 1}
    assert len(lineage["dataset_sha256"]) == 64
    assert "Resolve support request" not in serialized
    assert "Resolution number" not in serialized
    assert all(
        len(item["row_sha256"]) == 64
        for values in lineage["splits"].values()
        for item in values
    )


def test_strict_config_rejects_unknown_fields_and_resolves_output_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset": {"feature_scope": "support"},
                "output_root": "relative-output",
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(train.FineTunerConfigurationError, match="unexpected"):
        train.load_config(config_path)

    config_path.write_text(
        json.dumps(
            {"dataset": {"feature_scope": "support"}, "output_root": "relative-output"}
        ),
        encoding="utf-8",
    )
    config = train.load_config(config_path)
    assert config.output_root == (tmp_path / "relative-output").resolve()


def test_wandb_payload_and_errors_never_include_runtime_secrets(tmp_path: Path) -> None:
    config = _config(tmp_path)
    splits = train.split_golden_cases(
        [_case(index) for index in range(6)], config.dataset
    )
    lineage = train.dataset_lineage(splits)
    payload = json.dumps(train.wandb_config_payload(config, lineage))

    assert "POSTGRES_DSN" not in payload
    assert "HF_TOKEN" not in payload
    assert "WANDB_API_KEY" not in payload
    secret = "super-secret-token"
    assert secret not in train._safe_error_message(
        RuntimeError(f"provider rejected {secret}"), {"HF_TOKEN": secret}
    )


def test_memory_callback_logs_elapsed_and_cuda_metrics(monkeypatch) -> None:
    class FakeCuda:
        @staticmethod
        def is_available():
            return False

    class FakeTorch:
        cuda = FakeCuda()

    class FakeRun:
        def __init__(self):
            self.logged = []

        def log(self, values, *, step):
            self.logged.append((values, step))

    run = FakeRun()
    monkeypatch.setitem(sys.modules, "wandb", SimpleNamespace(run=run))
    deps = SimpleNamespace(torch=FakeTorch(), trainer_callback=object)
    callback = train._build_memory_callback(deps, 0.0)

    callback.on_log(None, SimpleNamespace(global_step=3), SimpleNamespace())

    values, step = run.logged[0]
    assert step == 3
    assert values["gpu/allocated_bytes"] == 0
    assert values["runtime/elapsed_seconds"] > 0


def test_quantization_config_uses_four_bit_nf4() -> None:
    _require_fine_tuner_stack()
    deps = train._import_ml_dependencies()

    quantization = train.build_quantization_config(
        train.QuantizationConfig(), deps
    )

    assert quantization.load_in_4bit is True
    assert quantization.bnb_4bit_quant_type == "nf4"
    assert quantization.bnb_4bit_use_double_quant is True


def _require_fine_tuner_stack():
    pytest.importorskip("torch")
    pytest.importorskip("datasets")
    pytest.importorskip("transformers")
    pytest.importorskip("peft")
    pytest.importorskip("trl")
    pytest.importorskip("safetensors")


def _write_tiny_llama(model_dir: Path) -> None:
    _require_fine_tuner_stack()
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast

    vocabulary = {
        token: index
        for index, token in enumerate(
            [
                "<unk>",
                "<s>",
                "</s>",
                "<pad>",
                "user",
                "assistant",
                "Resolve",
                "support",
                "request",
                "number",
                "safely",
                "Resolution",
                "is",
                "complete",
                ".",
            ]
            + [str(index) for index in range(10)]
        )
    }
    backend = Tokenizer(WordLevel(vocabulary, unk_token="<unk>"))
    backend.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend,
        unk_token="<unk>",
        bos_token="<s>",
        eos_token="</s>",
        pad_token="<pad>",
    )
    tokenizer.chat_template = (
        "{% for message in messages %}"
        "{{ '<s>' + message['role'] + '\\n' + message['content'] + '</s>' }}"
        "{% endfor %}"
    )
    model_dir.mkdir()
    tokenizer.save_pretrained(model_dir)
    model = LlamaForCausalLM(
        LlamaConfig(
            vocab_size=len(vocabulary),
            hidden_size=32,
            intermediate_size=64,
            num_hidden_layers=1,
            num_attention_heads=4,
            num_key_value_heads=2,
            max_position_embeddings=128,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id,
        )
    )
    model.save_pretrained(model_dir, safe_serialization=True)
    del model, tokenizer, backend
    gc.collect()


def test_cpu_pipeline_exports_adapter_and_lineage(tmp_path: Path) -> None:
    _require_fine_tuner_stack()
    model_dir = tmp_path / "tiny-llama"
    _write_tiny_llama(model_dir)
    config = _config(tmp_path, model_id=str(model_dir))

    result = train.run_training(
        config,
        cases=[_case(index) for index in range(6)],
        environ={},
    )

    assert (result.adapter_dir / "adapter_model.safetensors").is_file()
    assert (result.adapter_dir / "adapter_config.json").is_file()
    assert (result.run_dir / "resolved_config.json").is_file()
    assert any((result.run_dir / "checkpoints").glob("checkpoint-*"))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["dataset"]["counts"] == {"train": 4, "validation": 1, "test": 1}
    assert manifest["metrics"]["global_step"] == 1
    assert manifest["metrics"]["best_checkpoint"]
    assert all(item["sha256"] for item in manifest["artifacts"])


def test_cpu_pipeline_does_not_retain_memory_between_runs(tmp_path: Path) -> None:
    import tracemalloc

    _require_fine_tuner_stack()
    model_dir = tmp_path / "tiny-llama"
    _write_tiny_llama(model_dir)
    config = _config(tmp_path, model_id=str(model_dir))
    tracemalloc.start()
    retained = []
    for _ in range(2):
        result = train.run_training(
            config,
            cases=[_case(index) for index in range(6)],
            environ={},
        )
        assert (result.adapter_dir / "adapter_model.safetensors").is_file()
        gc.collect()
        retained.append(tracemalloc.get_traced_memory()[0])

    assert retained[1] - retained[0] <= 5 * 1024 * 1024


def test_cpu_pipeline_trains_with_gradient_checkpointing(tmp_path: Path) -> None:
    _require_fine_tuner_stack()
    model_dir = tmp_path / "tiny-llama"
    _write_tiny_llama(model_dir)
    config = _config(tmp_path, model_id=str(model_dir))
    config.training.gradient_checkpointing = True

    result = train.run_training(
        config,
        cases=[_case(index) for index in range(6)],
        environ={},
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["trainable_parameters"]["count"] > 0
    assert (result.adapter_dir / "adapter_model.safetensors").is_file()


@pytest.mark.gpu
def test_cuda_qlora_pipeline_does_not_retain_allocations_between_runs(
    tmp_path: Path,
) -> None:
    if os.getenv("RUN_FINE_TUNER_GPU_TESTS") != "1":
        pytest.skip("set RUN_FINE_TUNER_GPU_TESTS=1 to run the real CUDA QLoRA test")
    _require_fine_tuner_stack()
    torch = pytest.importorskip("torch")
    pytest.importorskip("bitsandbytes")
    if not torch.cuda.is_available():
        pytest.skip("CUDA is unavailable")

    model_dir = tmp_path / "tiny-llama"
    _write_tiny_llama(model_dir)
    config = _config(tmp_path, quantized=True, model_id=str(model_dir))
    retained = []
    for _ in range(2):
        result = train.run_training(
            config,
            cases=[_case(index) for index in range(6)],
            environ={},
        )
        assert (result.adapter_dir / "adapter_model.safetensors").is_file()
        gc.collect()
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        retained.append(torch.cuda.memory_allocated())

    assert retained[1] - retained[0] <= 2 * 1024 * 1024
