import { useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, AlertCircle, Search, Filter, Wrench, CheckCircle2, XCircle, ListFilter } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Disclaimer } from "@/components/ui/Disclaimer";
import { api, ApiError, type ScreenerResult } from "@/lib/api";
import { loadLlm, hasLlm } from "@/lib/llm";
import { cn } from "@/lib/utils";

const pctColor = (p: number) => (p > 0 ? "text-danger" : p < 0 ? "text-success" : "text-muted-foreground");
const yi = (v: number | null) => (v == null ? "—" : `${(v / 1e8).toFixed(2)} 亿`);

const EXAMPLES = [
  "半导体板块里，营收增长>30%、PE<30、机构覆盖>5家、最近1个月有研报上调评级、股东户数在减少的公司",
  "新能源车板块，PE<25、营收增速>20%、ROE>15%、主力资金净流入",
  "AI算力概念，市值>100亿、换手率>3%、毛利率>30%",
  "医药板块，PE<40、营收增长>10%、机构覆盖>3家",
];

export function Screener() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<ScreenerResult | null>(null);
  const [expandedStep, setExpandedStep] = useState<number | null>(null);

  const run = async () => {
    const q = query.trim();
    if (!q || loading) return;
    if (!hasLlm()) { setErr("请先接入 AI"); return; }
    setErr(null);
    setResult(null);
    setLoading(true);
    const llm = loadLlm();
    if (!llm) { setErr("请先接入 AI"); return; }
    try {
      const res = await api.screenerRun(q, llm);
      if (res.error) { setErr(res.error); return; }
      setResult(res);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "筛选失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="条件选股"
        subtitle="用自然语言描述筛选条件，AI 自动翻译为数据查询并执行"
        actions={
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Filter className="h-3.5 w-3.5" /> 自然语言 → 结构化筛选
          </div>
        }
      />

      {/* 输入区 */}
      <GlassCard className="mb-6">
        <div className="space-y-3">
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={3}
            placeholder="用自然语言描述筛选条件，例如：&#10;半导体板块里，营收增长>30%、PE<30、机构覆盖>5家"
            className="w-full resize-none rounded-lg border border-border bg-black/20 px-4 py-3 text-sm outline-none focus:border-primary/50"
            onKeyDown={(e) => { if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); run(); } }}
          />
          <div className="flex items-center justify-between">
            <div className="flex flex-wrap gap-1.5">
              {EXAMPLES.slice(0, 3).map((ex) => (
                <button
                  key={ex}
                  onClick={() => setQuery(ex)}
                  className="rounded-full border border-border/50 bg-muted/20 px-2.5 py-1 text-[11px] text-muted-foreground hover:border-primary/40 hover:text-primary"
                >
                  {ex.slice(0, 16)}…
                </button>
              ))}
            </div>
            <button
              onClick={run}
              disabled={loading || !query.trim()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25 disabled:opacity-50"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              {loading ? "筛选中…" : "开始筛选"}
            </button>
          </div>
        </div>
        {err && (
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" /> {err}
          </div>
        )}
        {!hasLlm() && !err && (
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3 text-sm text-muted-foreground">
            <AlertCircle className="h-4 w-4 shrink-0 text-warning" />
            还没接入 AI。<Link to="/settings" className="text-primary">先去接入你的 AI</Link>，条件解析需要 LLM。
          </div>
        )}
      </GlassCard>

      {/* 筛选链路可视化 */}
      {result && result.pipeline.length > 0 && (
        <GlassCard className="mb-4">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
            <ListFilter className="h-3.5 w-3.5" /> 筛选链路
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            {result.pipeline.map((step, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <button
                  onClick={() => setExpandedStep(expandedStep === i ? null : i)}
                  className={cn(
                    "inline-flex items-center gap-1 rounded-lg px-2 py-1 transition-colors",
                    step.error ? "bg-destructive/10 text-destructive" : "bg-primary/10 text-primary",
                  )}
                >
                  {step.error ? <XCircle className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
                  <span>{step.step}</span>
                  {step.count !== undefined && <span className="font-mono opacity-70">{step.count}</span>}
                  {step.output_count !== undefined && <span className="font-mono opacity-70">{step.output_count}</span>}
                </button>
                {i < result.pipeline.length - 1 && <span className="text-muted-foreground/30">→</span>}
              </div>
            ))}
          </div>
          {/* 展开的步骤详情 */}
          {expandedStep !== null && result.pipeline[expandedStep] && (() => {
            const st = result.pipeline[expandedStep];
            return (
              <div className="mt-2 rounded-lg bg-muted/20 p-2 text-xs text-muted-foreground">
                {st.industry && <p>行业范围：{st.industry}</p>}
                {st.input_count !== undefined && <p>输入：{st.input_count} 只</p>}
                {st.output_count !== undefined && <p>输出：{st.output_count} 只</p>}
                {st.conditions && st.conditions.length > 0 && <p>条件：{st.conditions.join("、")}</p>}
                {st.error && <p className="text-destructive">错误：{st.error}</p>}
              </div>
            );
          })()}
        </GlassCard>
      )}

      {/* 结果表格 */}
      {result && (
        <>
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-muted-foreground">
              筛选结果 <span className="text-xs font-normal text-muted-foreground/60">（共 {result.total} 条）</span>
            </h3>
            {result.criteria?.filters && result.criteria.filters.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {result.criteria.filters.filter((f: any) => f.field !== "industry" && f.field !== "concept").map((f: any, i: number) => (
                  <span key={i} className="inline-flex items-center gap-0.5 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                    <Wrench className="h-2.5 w-2.5" /> {f.field.replace(/_/g, " ")} = {f.value}
                  </span>
                ))}
              </div>
            )}
          </div>

          {result.results.length === 0 ? (
            <GlassCard>
              <p className="py-8 text-center text-sm text-muted-foreground">没有符合条件的股票</p>
            </GlassCard>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                    <th className="whitespace-nowrap px-3 py-2 font-medium">#</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">名称</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">现价</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">涨跌%</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">PE(TTM)</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">PB</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">总市值</th>
                    <th className="whitespace-nowrap px-3 py-2 font-medium">换手率%</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.map((s, i) => (
                    <tr key={s.code} className="border-b border-border/30 hover:bg-muted/10">
                      <td className="px-3 py-2 font-mono text-xs text-muted-foreground/50">{i + 1}</td>
                      <td className="px-3 py-2">
                        <Link to={`/stock-data?code=${s.code}`} className="font-medium text-primary hover:underline">
                          {s.name}
                        </Link>
                        <span className="ml-1 text-xs text-muted-foreground/50">{s.code}</span>
                      </td>
                      <td className="px-3 py-2 font-mono">{s.price}</td>
                      <td className={cn("px-3 py-2 font-mono", pctColor(s.change_pct))}>
                        {s.change_pct > 0 ? "+" : ""}{s.change_pct}%
                      </td>
                      <td className="px-3 py-2 font-mono">{s.pe_ttm ?? "—"}</td>
                      <td className="px-3 py-2 font-mono">{s.pb ?? "—"}</td>
                      <td className="px-3 py-2 font-mono">{yi(s.mcap_yi)}</td>
                      <td className="px-3 py-2 font-mono">{s.turnover_pct ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <Disclaimer />
    </div>
  );
}
