/**
 * Dashboard route composition.
 *
 * System role: maps stable browser locations to execution, observability, and
 * forensic workspaces within the shared operations shell.
 * Dependencies: React Router and page-level components.
 * Side effects: reads and updates browser history.
 */
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { ExecutionPage } from "./pages/ExecutionPage";
import { ForensicsPage } from "./pages/ForensicsPage";
import { ObservabilityPage } from "./pages/ObservabilityPage";

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<ExecutionPage />} />
          <Route path="observability" element={<ObservabilityPage />} />
          <Route path="forensics" element={<ForensicsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
