import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { formatNumber, formatTimestamp, todayIST } from "../format";
import type { Health, NewsItem, Trade, User, WatchlistItem } from "../types";
import { Column, DataTable } from "./DataTable";

type Tab = "news" | "watchlist" | "trades";

interface Props {
  token: string;
  user: User;
  onLogout: () => void;
  onSessionExpired: () => void;
}

export function Dashboard({ token, user, onLogout, onSessionExpired }: Props) {
  const [tab, setTab] = useState<Tab>("news");
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((data) => !cancelled && setHealth(data))
      .catch(() => !cancelled && setHealthError(true));
    return () => {
      cancelled = true;
    };
  }, []);

  // Wraps a call so a 401 (expired or invalid token) sends the user back to the login page.
  const guard = useCallback(
    (error: unknown): string => {
      if (error instanceof ApiError && error.status === 401) {
        onSessionExpired();
        return "Your session has ended. Please log in again.";
      }
      return error instanceof Error ? error.message : "Something went wrong.";
    },
    [onSessionExpired],
  );

  return (
    <div className="dashboard">
      <header className="topbar">
        <div>
          <strong>Stocks Trading Automation</strong>
          <span className="muted"> &middot; {user.email}</span>
        </div>
        <button onClick={onLogout}>Log out</button>
      </header>

      <div className="status-bar muted">
        {healthError && <span className="error">API unreachable</span>}
        {health && (
          <>
            <span>API: {health.status}</span>
            <span>Scheduler: {health.scheduler_running ? "running" : "stopped"}</span>
            {health.next_news_scrape && <span>Next news scrape: {formatTimestamp(health.next_news_scrape)}</span>}
          </>
        )}
      </div>

      <nav className="tabs" role="tablist">
        {(["news", "watchlist", "trades"] as Tab[]).map((name) => (
          <button key={name} role="tab" aria-selected={tab === name} className={tab === name ? "active" : ""} onClick={() => setTab(name)}>
            {name === "news" ? "News" : name === "watchlist" ? "Watchlist" : "Trades"}
          </button>
        ))}
      </nav>

      <main className="card">
        {tab === "news" && <NewsPanel token={token} onError={guard} />}
        {tab === "watchlist" && <WatchlistPanel token={token} onError={guard} />}
        {tab === "trades" && <TradesPanel token={token} onError={guard} />}
      </main>
    </div>
  );
}

interface PanelProps {
  token: string;
  onError: (error: unknown) => string;
}

function usePanelData<T>(load: () => Promise<T>, deps: unknown[], onError: PanelProps["onError"]) {
  const [rows, setRows] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    load()
      .then((data) => !cancelled && setRows(data))
      .catch((err) => !cancelled && setError(onError(err)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { rows, error };
}

function NewsPanel({ token, onError }: PanelProps) {
  const [limit, setLimit] = useState(50);
  const [materialOnly, setMaterialOnly] = useState(false);
  const { rows, error } = usePanelData<NewsItem[]>(
    () => api.news(token, limit, materialOnly),
    [token, limit, materialOnly],
    onError,
  );

  const columns: Column<NewsItem>[] = [
    { key: "time", label: "Published", render: (r) => formatTimestamp(r.timestamp) },
    { key: "ticker", label: "Ticker", render: (r) => r.ticker ?? "-" },
    {
      key: "title",
      label: "Headline",
      render: (r) =>
        r.url ? (
          <a href={r.url} target="_blank" rel="noreferrer">
            {r.title}
          </a>
        ) : (
          r.title
        ),
    },
    { key: "source", label: "Source", render: (r) => r.source },
    { key: "bias", label: "Bias", render: (r) => r.directional_bias ?? "-" },
    { key: "catalyst", label: "Catalyst", render: (r) => r.catalyst_type ?? "-" },
    { key: "confidence", label: "Confidence", render: (r) => formatNumber(r.sentiment_score) },
    { key: "material", label: "Material", render: (r) => (r.is_material ? "Yes" : "No") },
  ];

  return (
    <section>
      <div className="toolbar">
        <label>
          Headlines to show
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[10, 25, 50, 100, 250, 500].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={materialOnly} onChange={(e) => setMaterialOnly(e.target.checked)} />
          Material catalysts only
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {rows && (
        <DataTable
          rows={rows}
          columns={columns}
          rowKey={(r) => r.id}
          emptyText="No headlines yet. The news scrape runs hourly between 04:00 and 09:00 IST."
        />
      )}
    </section>
  );
}

function WatchlistPanel({ token, onError }: PanelProps) {
  const [date, setDate] = useState(todayIST());
  const { rows, error } = usePanelData<WatchlistItem[]>(() => api.watchlist(token, date), [token, date], onError);

  const columns: Column<WatchlistItem>[] = [
    { key: "rank", label: "#", render: (r) => r.rank },
    { key: "ticker", label: "Ticker", render: (r) => r.ticker },
    { key: "bias", label: "Bias", render: (r) => r.bias },
    { key: "catalyst", label: "Catalyst", render: (r) => r.catalyst_type },
    { key: "confidence", label: "Confidence", render: (r) => formatNumber(r.target_confidence) },
    { key: "headlines", label: "Headlines", render: (r) => r.supporting_headlines },
  ];

  return (
    <section>
      <div className="toolbar">
        <label>
          Trade date
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {rows && (
        <DataTable rows={rows} columns={columns} rowKey={(r) => r.rank} emptyText={`No watchlist for ${date}. It is built by the pre-market run.`} />
      )}
    </section>
  );
}

function TradesPanel({ token, onError }: PanelProps) {
  const [date, setDate] = useState(todayIST());
  const { rows, error } = usePanelData<Trade[]>(() => api.trades(token, date), [token, date], onError);

  const columns: Column<Trade>[] = [
    { key: "ticker", label: "Ticker", render: (r) => r.ticker },
    { key: "side", label: "Side", render: (r) => r.side },
    { key: "qty", label: "Qty", render: (r) => r.quantity, className: "num" },
    { key: "entry", label: "Entry", render: (r) => formatNumber(r.entry_price), className: "num" },
    { key: "exit", label: "Exit", render: (r) => formatNumber(r.exit_price), className: "num" },
    { key: "sl", label: "Stop", render: (r) => formatNumber(r.stop_loss), className: "num" },
    { key: "tp", label: "Target", render: (r) => formatNumber(r.take_profit), className: "num" },
    { key: "pnl", label: "PnL", render: (r) => formatNumber(r.pnl), className: "num" },
    { key: "status", label: "Status", render: (r) => r.status },
    { key: "mode", label: "Mode", render: (r) => r.mode },
  ];

  const totalPnl = rows?.reduce((sum, r) => sum + (r.pnl ?? 0), 0) ?? 0;

  return (
    <section>
      <div className="toolbar">
        <label>
          Trade date
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        {rows && rows.length > 0 && (
          <p className="metric">
            Realised PnL (INR): <strong>{formatNumber(totalPnl)}</strong>
          </p>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {rows && <DataTable rows={rows} columns={columns} rowKey={(r) => r.order_id} emptyText={`No trades for ${date}.`} />}
    </section>
  );
}
