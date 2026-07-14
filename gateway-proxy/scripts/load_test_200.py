#!/usr/bin/env python3
"""Send simultaneous requests through the CoreMesh gateway.

System role:
    Live verification of gateway token-bucket behavior under a synchronized
    burst; this is an operator tool, not part of the serving process.
Dependencies:
    Python standard library and already-running Redis, gateway, and upstream.
Side effects:
    Sends concurrent HTTP requests and writes a status summary to stdout/stderr;
    the gateway consequently mutates Redis rate-limit state.

The script is intentionally standard-library only so it can run from a fresh
Windows shell without extra Python packages.

Use a gateway proxy path (for example /health), not /healthz. The Python runtime
on :8000 must be running for successful upstream responses alongside 429s.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import sys
import urllib.error
import urllib.request


def send_request(url: str, team_id: str, timeout: float) -> int | str:
    """Send one identified request and normalize HTTP/network outcomes."""
    request = urllib.request.Request(url, headers={"X-Team-ID": team_id})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code
    except urllib.error.URLError as exc:
        return f"error:{exc.reason}"


def is_upstream_failure(status: int | str) -> bool:
    """Return whether a normalized outcome means no upstream was usable."""
    if isinstance(status, str):
        return status.startswith("error:")
    return status in {502, 503, 504}


def main() -> int:
    """Run the configured burst and fail unless rate limiting is observable."""
    parser = argparse.ArgumentParser(
        description="Fire simultaneous requests at the CoreMesh gateway proxy."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080/health",
        help="Gateway proxy URL (not /healthz). Default hits the rate-limited proxy path.",
    )
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--team-id", default="load-test-team")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.requests) as executor:
        futures = [
            executor.submit(send_request, args.url, args.team_id, args.timeout)
            for _ in range(args.requests)
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    counts = collections.Counter(results)
    print(f"URL: {args.url}")
    print(f"Requests: {args.requests}")
    for status, count in sorted(counts.items(), key=lambda item: str(item[0])):
        print(f"{status}: {count}")

    if counts.get(429, 0) == 0:
        print("Expected at least one 429 Too Many Requests response.", file=sys.stderr)
        return 1

    upstream_failures = sum(
        count for status, count in counts.items() if is_upstream_failure(status)
    )
    if upstream_failures == args.requests:
        print(
            "Every request failed upstream. Start Redis, the gateway on :8080, "
            "and the Python runtime on :8000 before running this script.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
