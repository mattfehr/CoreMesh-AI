/**
 * Responsive CoreMesh operations shell.
 *
 * System role: provides navigation, connection status, page context, and
 * accessible theme controls around every dashboard route.
 * Dependencies: React Router, TanStack Query, Lucide, and gateway metrics.
 * Side effects: polls gateway health and applies an in-memory theme preference.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  Bot,
  Command,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Sun,
} from "lucide-react";
import { coreMeshClient } from "../api/client";
import { toneForStatus } from "../lib/statusTone";
import { ThemeContext, type AppTheme } from "../lib/theme";
import { StatusBadge } from "./StatusBadge";

const navigation = [
  { to: "/", label: "Execution", icon: Bot, end: true },
  { to: "/observability", label: "Observability", icon: Activity },
  { to: "/forensics", label: "Forensics", icon: Network },
];

const pageContext: Record<string, { title: string; description: string }> = {
  "/": {
    title: "Execution Studio",
    description: "Route RAG, SQL, and supervisor tasks through the CoreMesh gateway.",
  },
  "/observability": {
    title: "Gateway Observability",
    description: "Live admission, cache, circuit, and upstream routing signals.",
  },
  "/forensics": {
    title: "Agent Forensics",
    description: "Inspect redacted OpenTelemetry execution trees and root causes.",
  },
};

export function AppLayout() {
  const location = useLocation();
  const context = pageContext[location.pathname] ?? pageContext["/"];
  const [collapsed, setCollapsed] = useState(false);
  const [theme, setTheme] = useState<AppTheme>(() => {
    return window.matchMedia?.("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  });
  const observability = useQuery({
    queryKey: ["observability"],
    queryFn: () => coreMeshClient.getObservability(),
    refetchInterval: 15_000,
    retry: 0,
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const gatewayState = useMemo(() => {
    if (observability.isError) return "unreachable";
    if (!observability.data) return "connecting";
    return observability.data.circuit_breaker.state;
  }, [observability.data, observability.isError]);

  return (
    <ThemeContext.Provider value={theme}>
      <div className={`app-shell${collapsed ? " sidebar-collapsed" : ""}`}>
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              <Command size={21} />
            </span>
            <div>
              <strong>CoreMesh</strong>
              <span>Control plane</span>
            </div>
          </div>

          <nav className="primary-nav" aria-label="Primary navigation">
            {navigation.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  title={collapsed ? item.label : undefined}
                >
                  <Icon size={18} aria-hidden="true" />
                  <span>{item.label}</span>
                </NavLink>
              );
            })}
          </nav>

          <div className="sidebar-status">
            <span className="sidebar-label">Gateway :8080</span>
            <StatusBadge
              tone={gatewayState === "unreachable" ? "danger" : toneForStatus(gatewayState)}
              pulse={gatewayState === "connecting"}
            >
              {gatewayState}
            </StatusBadge>
            <small>
              {observability.data
                ? `${observability.data.traffic.requests.toLocaleString()} requests since start`
                : "Waiting for operational snapshot"}
            </small>
          </div>

          <button
            className="sidebar-collapse"
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
            <span>{collapsed ? "Expand" : "Collapse"}</span>
          </button>
        </aside>

        <main className="main-shell">
          <header className="topbar">
            <div>
              <p className="eyebrow">Unified AI engineering interface</p>
              <h1>{context.title}</h1>
              <p>{context.description}</p>
            </div>
            <button
              className="icon-button theme-toggle"
              type="button"
              onClick={() => setTheme((value) => (value === "dark" ? "light" : "dark"))}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            >
              {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
            </button>
          </header>
          <div className="page-content">
            <Outlet />
          </div>
        </main>
      </div>
    </ThemeContext.Provider>
  );
}
