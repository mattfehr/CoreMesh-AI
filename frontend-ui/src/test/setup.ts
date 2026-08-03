/**
 * Shared Vitest browser-environment setup.
 *
 * System role: installs DOM matchers and clears rendered trees/storage between
 * isolated frontend tests.
 * Dependencies: Testing Library, Vitest, and JSDOM.
 * Side effects: resets browser-local test state after every case.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

Object.defineProperty(window, "sessionStorage", {
  configurable: true,
  value: new MemoryStorage(),
});
Object.defineProperty(Element.prototype, "scrollIntoView", {
  configurable: true,
  value: vi.fn(),
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});
