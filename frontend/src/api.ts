import type { Health, NewsItem, Trade, User, WatchlistItem } from "./types";

const TOKEN_KEY = "sta_access_token";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// localStorage can be unavailable (private mode, blocked storage). The app still works without it.
export function getStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function storeToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore: the user just has to log in again after a reload */
  }
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  params?: Record<string, string>;
  token?: string | null;
}

function describeError(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => (item && typeof item.msg === "string" ? item.msg : String(item))).join("; ");
  }
  return "Request failed";
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, params, token } = options;
  const query = params ? `?${new URLSearchParams(params).toString()}` : "";
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${path}${query}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check that it is running.");
  }

  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const data = await response.json();
      message = describeError(data.detail);
    } catch {
      /* body was not JSON; keep the status text */
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export const api = {
  register: (email: string, password: string) =>
    request<User>("/auth/register", { method: "POST", body: { email, password } }),

  login: (email: string, password: string) =>
    request<{ access_token: string; token_type: string }>("/auth/login", {
      method: "POST",
      body: { email, password },
    }),

  me: (token: string) => request<User>("/auth/me", { token }),

  health: () => request<Health>("/health"),

  news: (token: string, limit: number, materialOnly: boolean) =>
    request<NewsItem[]>("/news", {
      token,
      params: { limit: String(limit), material_only: String(materialOnly) },
    }),

  watchlist: (token: string, tradeDate: string) =>
    request<WatchlistItem[]>("/watchlist", { token, params: { trade_date: tradeDate } }),

  trades: (token: string, tradeDate: string) => request<Trade[]>("/trades", { token, params: { trade_date: tradeDate } }),
};
