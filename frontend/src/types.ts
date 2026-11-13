export interface User {
  id: number;
  email: string;
}

export interface NewsItem {
  id: number;
  timestamp: string;
  source: string;
  title: string;
  url: string;
  summary: string;
  ticker: string | null;
  sentiment_score: number | null;
  directional_bias: string | null;
  catalyst_type: string | null;
  is_material: boolean;
  rationale: string | null;
}

export interface WatchlistItem {
  trade_date: string;
  rank: number;
  ticker: string;
  bias: string;
  catalyst_type: string;
  target_confidence: number;
  supporting_headlines: number;
  generated_at: string | null;
}

export interface Trade {
  order_id: string;
  trade_date: string;
  mode: string;
  ticker: string;
  side: string;
  quantity: number;
  entry_price: number | null;
  exit_price: number | null;
  stop_loss: number;
  take_profit: number;
  pnl: number | null;
  status: string;
}

export interface Health {
  status: string;
  scheduler_running: boolean;
  next_news_scrape: string | null;
}
