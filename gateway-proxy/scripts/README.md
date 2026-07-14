# Gateway live-verification scripts

These operator tools use only the Python standard library. They send real
requests through a running gateway and can mutate Redis admission/cache state;
they are not unit tests and are not imported by the Go binary.

## Autopilot routing

~~~powershell
python scripts/verify_autopilot_routing.py --url http://localhost:8080/v1/chat/completions
~~~

The script submits one simple formatting prompt and one complex Go-debugging
prompt, then validates tier, rewritten model, and cache-policy response headers.
An upstream 4xx/5xx is still useful when routing headers are present. Disable
semantic caching unless the embedding API and Redis Stack index are also under
test.

Use <code>--expect-variant</code> to assert a configured experiment split.
Stable identity comes from the script's <code>--user-id</code>.

## Concurrent load

~~~powershell
python scripts/load_test_200.py --url http://localhost:8080/health --requests 200
~~~

Use a proxied path, never <code>/healthz</code>, because local health bypasses
admission. The default burst expects at least one 429 and at least one request
that is not a total upstream failure. Start Redis, gateway, and the Python
runtime first. Use a dedicated team ID so the test does not consume another
developer's rate-limit bucket.

Both commands return zero on success and nonzero when their assertions fail,
making them suitable for explicit smoke-test steps.
