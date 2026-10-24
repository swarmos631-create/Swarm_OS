/** Minimal fetch wrapper: same-origin cookies, CSRF double-submit header, typed errors. */

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  correlationId?: string;

  constructor(status: number, code: string, message: string, details?: unknown, correlationId?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.correlationId = correlationId;
  }
}

function csrfToken(): string | null {
  const m = document.cookie.match(/(?:^|;\s*)swarmos_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

type Query = Record<string, string | number | boolean | undefined | null>;

export async function api<T>(path: string, opts: { method?: string; body?: unknown; query?: Query; signal?: AbortSignal } = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const url = new URL(`/api/v1${path}`, window.location.origin);
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    const t = csrfToken();
    if (t) headers["X-CSRF-Token"] = t;
  }
  // A hung request (e.g. the API waiting on an unreachable database) must surface as an error, not an endless spinner.
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, REQUEST_TIMEOUT_MS);
  const onAbort = () => ctrl.abort();
  opts.signal?.addEventListener("abort", onAbort, { once: true });
  let res: Response;
  let text: string;
  try {
    res = await fetch(url, { method, headers, credentials: "same-origin", body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined, signal: ctrl.signal });
    text = await res.text();
  } catch (e) {
    if (timedOut) throw new ApiError(0, "timeout", "The SwarmOS API did not respond in time. Try again shortly.");
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "network_error", navigator.onLine ? "Cannot reach the SwarmOS API" : "You are offline");
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener("abort", onAbort);
  }
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const err = (data as any)?.error;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `The SwarmOS API failed (HTTP ${res.status})`, err?.details, err?.correlation_id);
  }
  // A 2xx that is not JSON (e.g. an HTML page from a misrouted request) must not be mistaken for data.
  if (data === BAD_RESPONSE) throw new ApiError(res.status, "bad_response", "The server returned an unexpected response (not the SwarmOS API)");
  return data as T;
}

const REQUEST_TIMEOUT_MS = 30_000;
const BAD_RESPONSE = Symbol("bad_response");

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return BAD_RESPONSE;
  }
}

export function newIdempotencyKey(): string {
  return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
