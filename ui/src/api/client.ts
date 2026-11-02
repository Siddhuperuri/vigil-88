import type { paths } from "./types.gen";

/** The shape of a successful JSON response, taken from the generated OpenAPI types. */
type JsonOf<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  responses: { 200: { content: { "application/json": infer R } } };
}
  ? R
  : never;

export type AuthStatus = JsonOf<"/api/v1/auth/status", "get">;

/** The engine answered, and said no. `status` is the HTTP status; `message` is safe to show. */
export class ApiError extends Error {
  readonly status: number;
  /** Seconds the engine asked the caller to wait (a lockout), when it said. */
  readonly retryAfterS: number | null;
  constructor(status: number, message: string, retryAfterS: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterS = retryAfterS;
  }
}

/** The engine could not be reached at all: not running, restarting, or the network is down. */
export class EngineUnreachableError extends Error {
  constructor() {
    super("The engine did not answer.");
    this.name = "EngineUnreachableError";
  }
}

// A custom header a cross-site form cannot set. The engine requires it on every state change
// (ADR 0016); sending it on reads as well keeps one code path.
const REQUEST_HEADER = { "X-Vigil-Request": "1" } as const;

async function detailOf(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const { detail } = body;
      if (typeof detail === "string") return detail;
    }
  } catch {
    // A body that is not JSON is fine: fall back to the status text below.
  }
  return response.statusText || `The engine answered ${String(response.status)}.`;
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      headers: REQUEST_HEADER,
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new EngineUnreachableError();
  }
  return readJson<T>(response);
}

function retryAfter(response: Response): number | null {
  const value = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  return Number.isFinite(value) ? value : null;
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new ApiError(response.status, await detailOf(response), retryAfter(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** POST, PATCH or DELETE with an optional JSON body. The engine requires the custom header. */
export async function sendJson<T>(
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: { ...REQUEST_HEADER, "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new EngineUnreachableError();
  }
  return readJson<T>(response);
}

export const getAuthStatus = (signal?: AbortSignal): Promise<AuthStatus> =>
  getJson<AuthStatus>("/api/v1/auth/status", signal);
