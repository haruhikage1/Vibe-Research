// Vibe-Research 后端 API 客户端。/api → vite 代理到本地 FastAPI（默认 8900）。
// 后端未启动或数据源异常时抛 ApiError，页面据此优雅降级。

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

// 后端访问密钥（对应后端部署时的 VR_API_KEY，公网部署防蹭用）。只存本地浏览器。
const ACCESS_KEY = "vr-access-key";

export function loadAccessKey(): string {
  try {
    return localStorage.getItem(ACCESS_KEY) || "";
  } catch {
    return "";
  }
}

export function saveAccessKey(key: string) {
  try {
    if (key) localStorage.setItem(ACCESS_KEY, key);
    else localStorage.removeItem(ACCESS_KEY);
  } catch {
    /* 隐私模式等场景 localStorage 不可用 */
  }
}

export function authHeaders(): Record<string, string> {
  const k = loadAccessKey();
  return k ? { Authorization: `Bearer ${k}` } : {};
}

export interface MyReport {
  id: string; name: string; industry: string; size: number; ext: string; ts: number;
}

export interface RagStatus {
  mode: string; indexed_chunks: number; indexed_reports: number; deps_missing: string[];
}

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
  tools?: { tool: string; args: Record<string, unknown> }[];
  ts?: number;
}
export interface Conversation {
  id: string; title: string; page: string; context_summary: string;
  messages: ConversationMessage[]; created_at: number; updated_at: number;
}
export interface ConversationListItem {
  id: string; title: string; page: string; context_summary: string;
  message_count: number; last_preview: string; created_at: number; updated_at: number;
}

// 下载/预览研报：带鉴权头 fetch → blob → 触发浏览器下载（<a download> 无法带 Authorization，故走 blob）。
export async function downloadReport(id: string, name: string): Promise<void> {
  const resp = await fetch(`/api/myreports/file/${id}`, { headers: authHeaders() });
  if (!resp.ok) throw new ApiError(`下载失败 HTTP ${resp.status}`, resp.status);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function request<T>(path: string, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown): Promise<T> {
  let resp: Response;
  const headers: Record<string, string> = { ...authHeaders() };
  const opts: RequestInit = { method };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (Object.keys(headers).length > 0) opts.headers = headers;
  try {
    resp = await fetch(`/api${path}`, opts);
  } catch {
    throw new ApiError("连接不到后端，请先启动 backend（uvicorn app:app --port 8900）", 0);
  }
  let payload: any = null;
  try {
    payload = await resp.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      throw new ApiError("后端开启了访问鉴权（VR_API_KEY）：请在「接入 AI」页底部填写后端访问密钥", 401);
    }
    throw new ApiError(payload?.detail || `HTTP ${resp.status}`, resp.status);
  }
  return (payload?.data ?? payload) as T;
}

const get = <T>(path: string) => request<T>(path, "GET");

export interface Quote {
  name: string; price: number; last_close: number; change_pct: number;
  pe_ttm: number; pb: number; mcap_yi: number; turnover_pct: number;
  limit_up: number; limit_down: number;
}

export interface Valuation {
  name: string; code: string; price: number; mcap_yi: number;
  pe_ttm: number; pb: number;
  eps_26e: number | null; eps_27e: number | null; pe_26e: number | null;
  cagr_pct: number | null; peg: number | null; digest_years: number | null;
  analyst_count: number; forecast_note?: string;
}

export interface Report {
  title: string; publishDate: string; orgSName: string;
  emRatingName?: string; indvInduName?: string; pdfUrl?: string | null;
}

export interface ValMetric {
  current: number; percentile: number; min: number; max: number;
  p20: number; p50: number; p80: number; n: number;
}
export interface ValPercentile {
  period: string; metrics: { pe_ttm?: ValMetric; pb?: ValMetric };
}

export interface Announcement {
  date: string; title: string; type: string; url: string;
}

export interface Financials {
  period: string | null;
  revenue: string | null; revenue_yoy: string | null;
  net_profit: string | null; net_profit_yoy: string | null;
  eps: string | null; bvps: string | null; roe: string | null;
  gross_margin: string | null; net_margin: string | null; op_cf_ps: string | null;
}

export interface NewsItem {
  新闻标题?: string; 发布时间?: string; 文章来源?: string; 新闻链接?: string;
}

export interface IndexQuote {
  name: string; price: number; change_pct: number; change_amt: number;
}

export interface MarketSentiment {
  up: number; down: number; flat: number; zt: number; zt_real: number; dt: number; dt_real: number;
  active: string; breadth: string; speculation: string; date: string;
}
export interface SectorFlow {
  name: string; pct: number; net: number; inflow: number; outflow: number; firms: number;
}
export interface MarketOverview {
  sentiment: MarketSentiment; sectors: SectorFlow[]; updated: string;
}

// 短线情绪：连板梯队 / 最高连板 / 炸板率 / 封板率 / 晋级率 / 涨跌停家数 + 连板股清单（客观公开榜单）
export interface EmotionTier { boards: number; count: number; plus: boolean }
export interface LianbanStock {
  code: string; name: string; boards: number;
  price: number; pct: number; amount: number | null; float_cap: number | null; industry: string;
}
export interface ShortTermEmotion {
  date: string;
  zt_count: number; dt_count: number; zb_count: number;
  max_boards: number; lianban_count: number;
  ladder: EmotionTier[];
  lianban_stocks: LianbanStock[];
  seal_rate: number | null; break_rate: number | null; promotion_rate: number | null;
  yzt_count: number;
}

// 全市场成交额榜（客观公开榜单）
export interface TurnoverStock {
  code: string; name: string;
  price: number | null; pct: number | null;
  amount: number | null; mcap: number | null; float_cap: number | null; industry: string;
}
export interface TurnoverTop { stocks: TurnoverStock[]; updated: string }

export interface RadarItem {
  title: string; url: string; time: string; source: string; summary?: string; zh?: string;
}
export interface Industry {
  key: string; name: string; accent: string; total: number; items: RadarItem[];
}
export interface RadarData {
  generated_at: string | null; recent_days: number; industries: Industry[];
  stats: { industries: number; total_sources: number; failed_sources?: number };
}

export interface Holding {
  code: string; name: string; price: number; shares: number; cost: number;
  market_value: number; pnl: number; pnl_pct: number;
}
export interface ClosedPosition {
  code: string; name: string; date: string; price: number; shares: number; cost: number;
  pnl: number; pnl_pct: number;
}
export interface PortfolioData {
  holdings: Holding[];
  totals: { market_value: number; cost: number; pnl: number; pnl_pct: number };
  closed: ClosedPosition[];
  realized_pnl: number;
  updated: string; last_refresh: string | null;
}

// 资金面 / 筹码 / 信号（v3.3 并入，均为「用户查的那只股」的公开数据）
export interface MarginRow { date: string; rzye: number; rzmre: number; rzche: number; rqye: number; rqmcl: number; rzrqye: number }
export interface BlockTradeRow { date: string; price: number; close: number; premium_pct: number; vol: number; amount: number; buyer: string; seller: string }
export interface HolderRow { date: string; holder_num: number; change_ratio: number; avg_shares: number }
export interface DividendRow { date: string; bonus_rmb: number; transfer_ratio: number; bonus_ratio: number | null; plan: string }
export interface FundFlowRow { date: string; main_net: number; small_net: number; mid_net: number; large_net: number; super_net: number }
export interface DtSeat { name: string; buy_amt: number; sell_amt: number; net: number }
export interface DragonTiger {
  records: { date: string; reason: string; net_buy: number; turnover: number }[];
  seats: { buy: DtSeat[]; sell: DtSeat[] };
  institution: { buy_amt: number; sell_amt: number; net_amt: number };
}
export interface LockupRow { date: string; type: string; shares: number; able_shares: number; ratio: number }
export interface Lockup { history: LockupRow[]; upcoming: LockupRow[] }
export interface Board { name: string; code: string; change_pct: number | string; lead_stock: string }
export interface Blocks { total: number; boards: Board[]; concept_tags: string[] }
export interface HotConcept { concept: string; bk: string; hit: number }
export interface QaRow { company: string; question: string; answer: string | null; answerer: string; ask_time: string }
export interface IndustryRow { rank: number; name: string; change_pct: number; code: string; up_count: number; down_count: number }
export interface IndustryData { top: IndustryRow[]; bottom: IndustryRow[]; total: number }

// 全球市场（美股 / 港股，移植自 global-stock-data · 东财域内源）
export interface GlobalIndex {
  key: string; name: string; region: string;
  price: number | null; change_pct: number | null;
}
export interface GlobalQuote {
  code: string; name: string;
  price: number | null; open: number | null; high: number | null; low: number | null;
  prev_close: number | null; amount: number | null; mcap: number | null; change_pct: number | null;
}
export interface GlobalMetrics {
  report_date: string;
  revenue: number | null; revenue_yoy: number | null; net_profit: number | null;
  eps: number | null; roe: number | null; gross_margin: number | null;
  net_margin: number | null; debt_ratio: number | null;
}
export interface GlobalStock {
  code: string; name: string; market: string;
  quote: GlobalQuote; metrics: GlobalMetrics | null;
}

// ---- 跨境信号映射 ----
export interface CrossStock {
  code: string; name: string; price: number | null; change_pct: number | null;
}
export interface CrossSector {
  name: string; change_pct: number | null;
  stocks: CrossStock[];
}
export interface CrossSignal {
  symbol: string; name: string; market: string;
  price: number | null; change_pct: number | null;
  direction: string; is_active: boolean; lag: string;
  sync_rate: number | null;
  sectors: CrossSector[];
}
export interface CrossSignalData {
  signals: CrossSignal[]; total: number; active_count: number; updated: string;
}
export interface CrossCorrelation {
  us_symbol: string; us_name: string; sector: string; lag: string; sync_rate: number | null;
}

// ---- 投资假设沙盒 ----
export interface ScenarioDefaults {
  code: string; name: string; period: string | null;
  price: number; pe_ttm: number | null; mcap_yi: number | null; shares_yi: number | null;
  revenue_yi: number | null; net_profit_yi: number | null; revenue_yoy: number | null;
  gross_margin: number | null; net_margin: number | null; op_expense_ratio: number | null;
  analyst_count: number;
  defaults: {
    revenue_growth: number | null; gross_margin: number | null; op_expense_ratio: number | null;
    target_pe: number | null; peg_threshold: number;
  };
}
export interface ScenarioResult {
  code: string; name: string;
  scenario: { revenue_growth: number; gross_margin: number; op_expense_ratio: number; target_pe: number; peg_threshold: number };
  base: {
    price: number; revenue_yi: number | null; net_profit_yi: number | null;
    gross_margin: number | null; net_margin: number | null; op_expense_ratio: number | null; pe_ttm: number | null;
  };
  projected: { revenue_yi: number; net_profit_yi: number; net_margin: number; eps: number | null };
  valuation: {
    implied_pe: number; implied_price: number | null; upside_pct: number | null;
    profit_growth_pct: number | null; scenario_peg: number | null; peg_threshold: number;
  };
  ai_commentary: string;
  ai_commentary_status: string;
  error?: string;
}

// ---- 投资复盘录 ----
export interface DailyBriefIndex {
  name: string; price: number; change_pct: number;
}
export interface DailyBriefGlobalIndex {
  name: string; region: string; price: number | null; change_pct: number | null;
}
export interface DailyBriefSentiment {
  up: number; down: number; zt: number; dt: number; breadth: string; speculation: string;
}
export interface DailyBriefEmotion {
  zt_count: number; dt_count: number; max_boards: number; lianban_count: number;
  seal_rate: number | null; break_rate: number | null;
}
export interface DailyBriefSector {
  name: string; pct: number; net: number;
}
export interface DailyBriefTurnover {
  name: string; pct: number | null; amount: number | null;
}
export interface DailyBriefHolding {
  code: string; name: string; pnl: number; pnl_pct: number;
}
export interface DailyBriefPortfolio {
  holdings: DailyBriefHolding[];
  totals: { market_value: number; cost: number; pnl: number; pnl_pct: number };
  realized_pnl: number;
}
export interface DailyBriefWatchItem {
  code: string; name: string; price: number; change_pct: number;
}
export interface DailyBriefNewsItem {
  industry: string; title: string; source: string; time: string;
}
export interface DailyBrief {
  date: string; generated_at: string;
  indices: DailyBriefIndex[];
  global_indices: DailyBriefGlobalIndex[];
  sentiment: DailyBriefSentiment | {};
  emotion: DailyBriefEmotion | {};
  sectors_top: DailyBriefSector[];
  turnover_top: DailyBriefTurnover[];
  portfolio: DailyBriefPortfolio;
  watchlist: DailyBriefWatchItem[];
  news_highlights: DailyBriefNewsItem[];
  ai_commentary: string;
  ai_commentary_status: string;
}
export interface DailyBriefListItem {
  date: string; generated_at: string; has_commentary: boolean;
  indices_count: number; holdings_count: number;
}

// ---- 自然语言筛选器 ----
export interface ScreenerCriteria {
  industry?: string;
  concept?: string;
  filters?: { field: string; value: unknown }[];
  error?: string;
}
export interface ScreenerPipelineStep {
  step: string;
  count?: number;
  input_count?: number;
  output_count?: number;
  conditions?: string[];
  industry?: string;
  error?: string;
}
export interface ScreenerStock {
  code: string; name: string; price: number; change_pct: number;
  pe_ttm: number | null; pb: number | null;
  mcap_yi: number | null; turnover_pct: number | null;
}
export interface ScreenerResult {
  criteria: ScreenerCriteria;
  results: ScreenerStock[];
  total: number;
  pipeline: ScreenerPipelineStep[];
  error?: string;
}

export const api = {
  health: () => get<{ ok: boolean }>("/health"),
  indices: () => get<IndexQuote[]>("/indices"),
  marketOverview: () => get<MarketOverview>("/market/overview"),
  emotion: () => get<ShortTermEmotion>("/market/emotion"),
  turnoverTop: () => get<TurnoverTop>("/market/turnover-top"),
  globalIndices: () => get<GlobalIndex[]>("/global/indices"),
  globalStock: (symbol: string) => get<GlobalStock>(`/global/stock?symbol=${encodeURIComponent(symbol)}`),
  crossMarketSignals: () => get<CrossSignalData>("/cross-market/signals"),
  crossMarketCorrelation: () => get<CrossCorrelation[]>("/cross-market/correlation"),
  scenarioDefaults: (code: string) => get<ScenarioDefaults>(`/scenario/defaults?code=${code}`),
  scenarioCompute: (
    code: string,
    adj: { revenue_growth: number | null; gross_margin: number | null; op_expense_ratio: number | null; target_pe: number | null; peg_threshold: number | null },
    llm?: any,
  ) => request<ScenarioResult>("/scenario", "POST", { code, ...adj, llm }),
  radar: () => get<RadarData>("/radar"),
  radarRefresh: () => request<RadarData>("/radar/refresh", "POST"),
  portfolio: () => get<PortfolioData>("/portfolio"),
  addHolding: (code: string, shares: number, cost: number) => request<PortfolioData>("/portfolio/holding", "POST", { code, shares, cost }),
  removeHolding: (code: string) => request<PortfolioData>(`/portfolio/holding?code=${code}`, "DELETE"),
  refreshPortfolio: () => request<PortfolioData>("/portfolio/refresh", "POST"),
  closePosition: (code: string, date: string, price: number, shares: number, cost: number) =>
    request<PortfolioData>("/portfolio/close", "POST", { code, date, price, shares, cost }),
  removeClosed: (index: number) => request<PortfolioData>(`/portfolio/close?index=${index}`, "DELETE"),
  valuation: (code: string) => get<Valuation>(`/valuation?code=${code}`),
  percentile: (code: string) => get<ValPercentile>(`/valuation/percentile?code=${code}`),
  financials: (code: string) => get<Financials>(`/financials?code=${code}`),
  announcements: (code: string) => get<Announcement[]>(`/announcements?code=${code}`),
  quote: (codes: string) => get<Record<string, Quote>>(`/quote?codes=${codes}`),
  reports: (code: string) => get<Report[]>(`/reports?code=${code}`),
  news: (code: string) => get<NewsItem[]>(`/news?code=${code}`),
  margin: (code: string) => get<MarginRow[]>(`/margin?code=${code}`),
  blockTrade: (code: string) => get<BlockTradeRow[]>(`/block-trade?code=${code}`),
  holders: (code: string) => get<HolderRow[]>(`/holders?code=${code}`),
  dividend: (code: string) => get<DividendRow[]>(`/dividend?code=${code}`),
  fundFlow: (code: string) => get<FundFlowRow[]>(`/fund-flow?code=${code}`),
  dragonTiger: (code: string) => get<DragonTiger>(`/dragon-tiger?code=${code}`),
  lockup: (code: string) => get<Lockup>(`/lockup?code=${code}`),
  blocks: (code: string) => get<Blocks>(`/blocks?code=${code}`),
  hotConcepts: (code: string) => get<HotConcept[]>(`/hot-concepts?code=${code}`),
  investorQa: (code: string) => get<QaRow[]>(`/investor-qa?code=${code}`),
  industry: (top = 20) => get<IndustryData>(`/industry?top=${top}`),
  myReports: () => get<MyReport[]>("/myreports"),
  uploadReport: (name: string, contentB64: string) =>
    request<MyReport>("/myreports", "POST", { name, content_b64: contentB64 }),
  deleteReport: (id: string) => request<{ ok: boolean }>(`/myreports/${id}`, "DELETE"),
  ragStatus: () => get<RagStatus>("/rag/status"),
  ragReindex: () => request<{ reindexed: number; details: { report_id: string; chunks: number; skipped: string | null }[] }>("/rag/reindex", "POST"),
  ragIndexOne: (id: string) => request<{ report_id: string; chunks: number; skipped: string | null }>(`/rag/index/${id}`, "POST"),
  // ---- 投资复盘录 ----
  dailyBriefGenerate: (watchCodes: string[], llm?: any) =>
    request<DailyBrief>("/daily-brief/generate", "POST", { watch_codes: watchCodes, llm }),
  dailyBriefCommentary: (llm: any) =>
    request<DailyBrief>("/daily-brief/commentary", "POST", { llm }),
  dailyBriefGet: (date?: string) => get<DailyBrief>(`/daily-brief${date ? `?date=${date}` : ""}`),
  dailyBriefHistory: () => get<DailyBriefListItem[]>("/daily-brief/history"),

  screenerRun: (query: string, llm: any) =>
    request<ScreenerResult>("/screener", "POST", { query, llm }),

  conversations: (page?: string) => get<ConversationListItem[]>(`/conversations${page ? `?page=${page}` : ""}`),
  conversation: (id: string) => get<Conversation>(`/conversations/${id}`),
  createConversation: (page: string, context: string, firstMessage: string) =>
    request<Conversation>("/conversations", "POST", { page, context, first_message: firstMessage }),
  appendMessage: (id: string, role: string, content: string, tools?: ConversationMessage["tools"]) =>
    request<Conversation>(`/conversations/${id}/messages`, "POST", { role, content, tools }),
  deleteConversation: (id: string) => request<{ ok: boolean }>(`/conversations/${id}`, "DELETE"),
  clearConversations: (page?: string) => request<{ deleted: number }>(`/conversations${page ? `?page=${page}` : ""}`, "DELETE"),
};
