"use client";

import { useCallback } from "react";

import { useAuthState } from "@/lib/auth-context";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function detail(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((e) => (e && typeof e === "object" && "msg" in e ? String(e.msg) : String(e)))
        .join("; ");
    }
  }
  return `Request failed (${status})`;
}

type Options = { method?: string; json?: unknown; form?: FormData; auth?: boolean };

/** A fetcher for the workbench API that adds whichever auth headers are in play. */
export function useApi() {
  const auth = useAuthState();
  return useCallback(
    async <T,>(path: string, opts: Options = {}): Promise<T> => {
      const headers: Record<string, string> = opts.auth === false ? {} : await auth.authHeaders();
      let body: BodyInit | undefined;
      if (opts.json !== undefined) {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(opts.json);
      } else if (opts.form) {
        body = opts.form;
      }
      const res = await fetch(path, {
        method: opts.method ?? (body ? "POST" : "GET"),
        headers,
        body,
        cache: "no-store",
      });
      const text = await res.text();
      const parsed: unknown = text ? safeJson(text) : null;
      if (!res.ok) throw new ApiError(res.status, detail(parsed, res.status));
      return parsed as T;
    },
    [auth],
  );
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
