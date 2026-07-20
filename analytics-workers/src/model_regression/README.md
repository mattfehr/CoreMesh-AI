# Model regression evaluator

This package owns the Phase 4.2 CI evaluation runner. It reads
`golden_datasets`, sends each case through the configured gateway API, writes
JSON/Markdown/HTML reports, and compares baseline versus candidate accuracy.

Default CI rows use deterministic gateway-header scoring plus one
`exact_json` chat-stub case so the GitHub Action can run without
model-provider credentials (`COREMESH_CHAT_STUB=true`). Production-mined rows
without an explicit `expected_output.scoring` block use `llm_judge` scoring
and require `OPENAI_API_KEY`.

Compare fails when candidate overall accuracy is below `--min-accuracy`
(default `0.90` / `REGRESSION_MIN_ACCURACY`) or when overall/per-scope
accuracy drops by more than `--max-drop` (default `0.03`).
