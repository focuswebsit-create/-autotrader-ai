"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getBackend } from "@/lib/client";
import type { Result } from "@/lib/types";

/** Poll a backend GET endpoint; keeps the last good value and the latest error. */
export function usePoll<T>(path: string, everyMs: number, params?: Record<string, string>) {
  const key = params ? JSON.stringify(params) : "";
  const [state, setState] = useState<{ data: T | null; error: string | null; at: number | null }>({
    data: null,
    error: null,
    at: null,
  });
  // Responses can arrive out of order; only the latest request may update state.
  const seq = useRef(0);
  const run = useCallback(async () => {
    const mine = ++seq.current;
    const res: Result<T> = await getBackend<T>(path, key ? JSON.parse(key) : undefined);
    if (mine !== seq.current) return;
    setState((prev) =>
      res.ok ? { data: res.data, error: null, at: Date.now() } : { ...prev, error: res.error },
    );
  }, [path, key]);
  useEffect(() => {
    // First tick from a timer callback (not synchronously in the effect body).
    const first = window.setTimeout(() => void run(), 0);
    const id = window.setInterval(() => void run(), everyMs);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [run, everyMs]);
  return state;
}

/** Re-render periodically so "x seconds ago" labels stay correct. */
export function useNow(everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}
