# Forensic tracing placeholder

This directory is reserved for the Phase 3.3 OpenTelemetry forensics layer. It
currently contains only <code>.gitkeep</code>; there is no Python package,
instrumentation hook, exporter, trace schema, or runtime integration.

The OpenTelemetry libraries in <code>requirements.txt</code> do not make
tracing active by themselves.

A future implementation must document span boundaries, correlation IDs,
attribute redaction, prompt/response capture policy, exporter configuration,
sampling, failure behavior, and retention. Instrumentation must not silently
place sensitive document or model content in telemetry.
