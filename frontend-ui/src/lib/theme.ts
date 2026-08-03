/** Shared in-memory application theme contract. */
import { createContext, useContext } from "react";

export type AppTheme = "dark" | "light";

export const ThemeContext = createContext<AppTheme>("dark");

export function useAppTheme(): AppTheme {
  return useContext(ThemeContext);
}
