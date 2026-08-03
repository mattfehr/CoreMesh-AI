/**
 * CoreMesh React application bootstrap.
 *
 * System role: installs server-state caching, routing, and global dashboard
 * styles around the browser application.
 * Dependencies: React 19, TanStack Query, React Router, and React Flow CSS.
 * Side effects: mounts the SPA into #root and starts browser event handling.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@xyflow/react/dist/style.css";
import { App } from "./App";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 2_000,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
});

const root = document.getElementById("root");
if (!root) {
  throw new Error("CoreMesh frontend root element was not found.");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
