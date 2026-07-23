# Fine-tuning pipeline

This directory implements the Phase 4.3 offline customization boundary. It
reads one reviewed feature scope from PostgreSQL, creates deterministic
train/validation/test partitions, trains a PEFT LoRA adapter, and writes a
lineage manifest beside adapter-only safetensors. It does not modify the
golden dataset, run broad benchmarks, promote adapters, or deploy vLLM.

## Files and data contract

- `train.py` owns strict configuration, read-only dataset loading, PEFT/QLoRA
  training, W&B telemetry, checkpoint selection, cleanup, and manifests.
- `config.example.json` is the non-secret production configuration template.
- `__init__.py` marks the import boundary and performs no model loading.

Every selected `golden_datasets` row must share the configured
`feature_scope`, have a unique case identity and prompt/completion pair, and
provide a non-empty `expected_output.reference_answer`. At least three valid
rows are required. The seeded 80/10/10 split is recorded by case ID,
fingerprint, provenance, and row hash. Prompts and reference answers are not
copied into the manifest. The test partition is reserved for the future
evaluation phase and is never passed to the trainer.

## Installation and credentials

Use Python 3.11 or newer in a dedicated environment. Install a CUDA-enabled
PyTorch 2.13 wheel appropriate for the host, then install the isolated stack:

~~~powershell
python -m venv .venv-fine-tuner
.venv-fine-tuner\Scripts\Activate.ps1
python -m pip install -r requirements-fine-tuner.txt
~~~

The default model is the gated
`meta-llama/Meta-Llama-3-8B-Instruct` revision
`8afb486c1db24fe5011ec46dfbe5b5dccdb575c2`. Accept the model license in
Hugging Face before setting `HF_TOKEN`. Configure these only as environment
variables; they are never accepted in the JSON config or stored in artifacts:

| Variable | Required when | Purpose |
| --- | --- | --- |
| `POSTGRES_DSN` | Production dataset loading | Read-only `golden_datasets` connection. |
| `HF_TOKEN` | Using the default gated model | Model configuration, tokenizer, and weights. |
| `WANDB_API_KEY` | `wandb.mode` is `online` | Remote experiment logging. |

Copy and edit the example into the `analytics-workers` root so `output_root` resolves
to `analytics-workers/artifacts/fine_tuner`:

~~~powershell
Copy-Item src/fine_tuner/config.example.json fine-tuner.json
python -m src.fine_tuner.train --config fine-tuner.json
~~~

The command exits `0` and prints `manifest.json` on success. Configuration,
dataset, credential, model-access, CUDA, and bitsandbytes preflight errors exit
`2`. Training or export failures that create a run directory exit `1` and leave
`manifest.failed.json`.

## Training and artifacts

Production mode loads the pinned base model in four-bit NF4 with double
quantization, prepares it for k-bit training, and trains only `q_proj` and
`v_proj` LoRA weights at rank 16, alpha 32, and dropout 0.05. It uses
completion-only loss, gradient checkpointing, epoch-aligned validation and
checkpoints, early stopping on validation loss, and at most two retained
checkpoints. Set `quantization.enabled` to `false` only for CPU debugging or
the portable tiny-model test.

Each run creates a never-overwritten directory below
`artifacts/fine_tuner/<feature-scope>/` with:

~~~text
<run-id>/
|-- adapter/
|   |-- adapter_config.json
|   |-- adapter_model.safetensors
|   `-- tokenizer metadata
|-- checkpoints/
|-- resolved_config.json
`-- manifest.json
~~~

The manifest ties the adapter to exact source-row hashes, split membership,
model revision, resolved configuration, package versions, Git state, hardware,
loss and checkpoint metrics, CUDA allocation peaks, W&B identity, and hashes
for every exported artifact. A failed run never contains a final adapter.

## W&B and memory behavior

`online` sends Trainer metrics and custom CUDA allocation/reservation metrics
to W&B. `offline` creates a local W&B run without network traffic. `disabled`
does not initialize W&B. All modes close their run handles, release trainer,
model, and tokenizer references, collect cyclic garbage, and empty unused CUDA
cache blocks in a `finally` path.

Run the portable tests after installing fine-tuner dependencies. The CPU suite
trains twice in one process and fails if traced memory grows by more than 5 MiB.
The real QLoRA test builds a tiny local Llama model, requires no downloads, and
is explicitly opt-in:

~~~powershell
python -m pytest -q tests/test_fine_tuner.py -m "not gpu"
$env:RUN_FINE_TUNER_GPU_TESTS='1'
python -m pytest -q tests/test_fine_tuner.py -m gpu
~~~

The CUDA test trains twice in one process and fails if cleaned allocated memory
grows by more than 2 MiB.
