"""CoreMesh parameter-efficient fine-tuning pipeline.

System role:
    Converts reviewed golden-dataset cases into deployable LoRA adapters.
Dependencies:
    The training entry point lazily loads the isolated Hugging Face ML stack.
Side effects:
    Importing this package has no side effects; ``train`` performs database,
    model-download, experiment-tracking, GPU, and artifact I/O when invoked.
"""

