/**
 * Deployment-contract checks for the static frontend container.
 *
 * BusyBox wget resolves localhost to IPv6 first, while the Nginx deployment
 * listener is IPv4-only. Keep the Compose probe on explicit IPv4 loopback so
 * a healthy frontend is not rejected before the integration stages begin.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const frontendRoot = process.cwd();
const nginxConfig = readFileSync(resolve(frontendRoot, "nginx.conf"), "utf8");
const dockerfile = readFileSync(resolve(frontendRoot, "Dockerfile"), "utf8");
const composeConfig = readFileSync(
  resolve(frontendRoot, "..", "docker-compose.yml"),
  "utf8",
);

describe("frontend container deployment contract", () => {
  it("probes the IPv4 loopback address on the Nginx listener port", () => {
    const listenerPort = nginxConfig.match(/^\s*listen\s+(\d+);/m)?.[1];

    expect(listenerPort).toBe("3000");
    expect(dockerfile).toContain(
      `wget -qO- http://127.0.0.1:${listenerPort}/`,
    );
    expect(composeConfig).toContain(
      `wget -qO- http://127.0.0.1:${listenerPort}/`,
    );
  });
});
