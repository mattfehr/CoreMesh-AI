#!/usr/bin/env python3
"""Verify CoreMesh autopilot model routing through the gateway.

The script is intentionally standard-library only. It treats any HTTP response
as usable if the gateway adds autopilot headers, so the upstream runtime does
not need to implement chat completions for the routing check to be meaningful.

Local smoke-test note: LLM API keys are not required for this script when
SEMANTIC_CACHE_ENABLED=false. If responses show variant=baseline with
reason=experiment_lookup_failed and experiment_error contains "failed SASL
auth", the router is working but the gateway cannot authenticate to the local
Docker Postgres instance. Fix the local POSTGRES_DSN/password or recreate the
postgres volume, then restart the gateway so its connection pool is rebuilt.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    expected_tier: str
    expected_model: str
    expected_cache_policy: str


@dataclass(frozen=True)
class Result:
    status: int | str
    headers: dict[str, str]
    body: str


def send(url: str, user_id: str, case: Case, timeout: float) -> Result:
    payload = {
        "model": "client-selected-model",
        "messages": [{"role": "user", "content": case.prompt}],
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-User-ID": user_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return Result(response.status, dict(response.headers.items()), body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return Result(exc.code, dict(exc.headers.items()), body)
    except urllib.error.URLError as exc:
        return Result(f"error:{exc.reason}", {}, "")


def header(headers: dict[str, str], name: str) -> str:
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return ""


def validate(case: Case, result: Result) -> list[str]:
    failures: list[str] = []
    tier = header(result.headers, "X-CoreMesh-Autopilot-Tier")
    model = header(result.headers, "X-CoreMesh-Routed-Model")
    cache_policy = header(result.headers, "X-CoreMesh-Cache-Policy")

    if not tier:
        failures.append("missing X-CoreMesh-Autopilot-Tier")
    elif tier != case.expected_tier:
        failures.append(f"tier {tier!r}, want {case.expected_tier!r}")

    if not model:
        failures.append("missing X-CoreMesh-Routed-Model")
    elif model != case.expected_model:
        failures.append(f"model {model!r}, want {case.expected_model!r}")

    if not cache_policy:
        failures.append("missing X-CoreMesh-Cache-Policy")
    elif cache_policy != case.expected_cache_policy:
        failures.append(
            f"cache policy {cache_policy!r}, want {case.expected_cache_policy!r}"
        )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify CoreMesh autopilot simple-vs-complex routing headers."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080/v1/chat/completions",
        help="Gateway chat/completions URL.",
    )
    parser.add_argument("--user-id", default="autopilot-verify-user")
    parser.add_argument("--tier1-model", default="gpt-4o-mini")
    parser.add_argument("--tier3-model", default="gpt-4o")
    parser.add_argument(
        "--expect-variant",
        choices=("none", "experimental", "baseline"),
        help=(
            "Expected experiment variant. When set to baseline, both cases are "
            "expected to route to tier-3."
        ),
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    cases = [
        Case(
            name="simple_formatting",
            prompt=(
                "Reformat this contact into JSON with name and phone fields: "
                "Ada Lovelace, 555-0100."
            ),
            expected_tier="tier-1",
            expected_model=args.tier1_model,
            expected_cache_policy="allow",
        ),
        Case(
            name="complex_code_logic",
            prompt=(
                "Analyze this Go concurrency bug, debug the root cause, and compare "
                "two safe fixes with tradeoffs:\n```go\nfunc worker(ch chan int) { "
                "close(ch); ch <- 1 }\n```"
            ),
            expected_tier="tier-3",
            expected_model=args.tier3_model,
            expected_cache_policy="bypass",
        ),
    ]

    failed = False
    for case in cases:
        result = send(args.url, args.user_id, case, args.timeout)
        if args.expect_variant == "baseline":
            case = Case(
                name=case.name,
                prompt=case.prompt,
                expected_tier="tier-3",
                expected_model=args.tier3_model,
                expected_cache_policy=case.expected_cache_policy,
            )
        failures = validate(case, result)
        tier = header(result.headers, "X-CoreMesh-Autopilot-Tier") or "-"
        model = header(result.headers, "X-CoreMesh-Routed-Model") or "-"
        variant = header(result.headers, "X-CoreMesh-Experiment-Variant") or "-"
        cache_policy = header(result.headers, "X-CoreMesh-Cache-Policy") or "-"
        reason = header(result.headers, "X-CoreMesh-Autopilot-Reason") or "-"
        experiment_error = header(result.headers, "X-CoreMesh-Experiment-Error") or "-"

        if args.expect_variant and variant != args.expect_variant:
            failures.append(f"variant {variant!r}, want {args.expect_variant!r}")

        print(
            f"{case.name}: status={result.status} tier={tier} "
            f"model={model} variant={variant} cache={cache_policy} "
            f"reason={reason} experiment_error={experiment_error}"
        )
        if failures:
            failed = True
            for failure in failures:
                print(f"  FAIL: {failure}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
