import { useState, useRef, useEffect } from "react";
import { Link } from "react-router-dom";
import { Sparkles, X, Settings, Send, Loader2, Wrench, AlertCircle, Plus, History } from "lucide-react";
import { cn } from "@/lib/utils";
import { hasLlm, chatStream, type ChatMsg } from "@/lib/llm";
import { api, ApiError, type ConversationListItem } from "@/lib/api";
import { SaveNoteButton } from "@/components/ui/SaveNoteButton";

interface Props {
  context: string;
  suggestions?: string[];
  label?: string;
  page: string;
}

const TOOL_LABEL: Record<string, string> = {
  query_quote: "查行情",
  query_valuation: "查估值",
  query_reports: "查研报",
  query_news: "查新闻",
  query_global_stock: "查美港股",
  query_reports_rag: "查研报库",
};

const argStr = (a: Record<string, unknown>): string => {
  if (Array.isArray(a.codes)) return (a.codes as unknown[]).join(",");
  if (typeof a.code === "string") return a.code;
  if (typeof a.query === "string") return a.query.slice(0, 12);
  return "";
};

interface ToolUse { name: string; arg: string }

export function AskAiButton({ context, suggestions = [], label = "问 AI", page }: Props) {
  const [open, setOpen] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [msgs, setMsgs] = useState<(ChatMsg & { tools?: ToolUse[] })[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [historyList, setHistoryList] = useState<ConversationListItem[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (open) setConfigured(hasLlm());
  }, [open]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const loadHistory = async () => {
    try {
      setHistoryList(await api.conversations(page));
    } catch {
      setHistoryList([]);
    }
  };

  const restoreConversation = async (cid: string) => {
    try {
      const conv = await api.conversation(cid);
      setConversationId(conv.id);
      setMsgs(conv.messages.map((m) => ({
        role: m.role,
        content: m.content,
        tools: m.tools?.map((t) => ({ name: t.tool, arg: argStr(t.args as Record<string, unknown>) })),
      })));
      setShowHistory(false);
    } catch {
      setErr("加载对话失败");
    }
  };

  const startNewConversation = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setConversationId(null);
    setMsgs([]);
    setErr(null);
    setShowHistory(false);
  };

  useEffect(() => {
    if (open && configured) {
      loadHistory();
      if (!conversationId && msgs.length === 0) {
        (async () => {
          try {
            const list = await api.conversations(page);
            const recent = list[0];
            if (recent && Date.now() - recent.updated_at < 86400000 && recent.message_count > 1) {
              await restoreConversation(recent.id);
            }
          } catch {
            /* 静默 */
          }
        })();
      }
    }
  }, [open, configured]);

  const close = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setOpen(false);
    setShowHistory(false);
  };

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, loading]);

  const send = async (text: string) => {
    const q = text.trim();
    if (!q || loading) return;
    setInput("");
    setErr(null);
    const history: ChatMsg[] = [...msgs.map(({ role, content }) => ({ role, content })), { role: "user", content: q }];
    setMsgs((m) => [...m, { role: "user", content: q }, { role: "assistant", content: "", tools: [] }]);
    setLoading(true);
    const patchLast = (fn: (msg: ChatMsg & { tools?: ToolUse[] }) => ChatMsg & { tools?: ToolUse[] }) =>
      setMsgs((m) => m.map((msg, i) => (i === m.length - 1 && msg.role === "assistant" ? fn(msg) : msg)));
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    const alive = () => abortRef.current === ac && !ac.signal.aborted;

    let cid = conversationId;
    if (!cid) {
      try {
        const conv = await api.createConversation(page, context.slice(0, 200), q);
        cid = conv.id;
        setConversationId(cid);
      } catch {
        /* 持久化失败不阻塞对话 */
      }
    } else {
      try {
        await api.appendMessage(cid, "user", q);
      } catch {
        /* 同上 */
      }
    }

    try {
      const result = await chatStream(history, context, {
        onTool: (tool, args) => { if (alive()) patchLast((msg) => ({ ...msg, tools: [...(msg.tools || []), { name: tool, arg: argStr(args) }] })); },
        onDelta: (t) => { if (alive()) patchLast((msg) => ({ ...msg, content: msg.content + t })); },
      }, ac.signal);
      if (cid) {
        try {
          await api.appendMessage(cid, "assistant", result.content,
            result.trace.map((t) => ({ tool: t.tool, args: t.args })));
        } catch {
          /* 同上 */
        }
      }
    } catch (e) {
      setMsgs((m) => m.filter((msg, i) => !(i === m.length - 1 && msg.role === "assistant" && !msg.content)));
      if (!ac.signal.aborted) setErr(e instanceof ApiError ? e.message : "对话失败");
    } finally {
      if (abortRef.current === ac) {
        abortRef.current = null;
        setLoading(false);
      }
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-3 py-1.5 text-sm font-medium text-primary shadow-glow transition-colors hover:bg-primary/25"
      >
        <Sparkles className="h-4 w-4" />
        {label}
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex justify-end">
          <div className="absolute inset-0 bg-black/50" onClick={close} />
          <aside className="glass relative m-3 flex w-full max-w-md flex-col rounded-2xl">
            <div className="flex items-center justify-between border-b border-border/60 p-4">
              <span className="flex items-center gap-2 font-semibold text-glow">
                <Sparkles className="h-4 w-4 text-primary" /> 问 AI · 本页上下文
              </span>
              <div className="flex items-center gap-1.5">
                {configured && (
                  <>
                    <button
                      onClick={startNewConversation}
                      className="rounded-lg p-1.5 text-muted-foreground hover:text-primary"
                      title="新对话"
                    >
                      <Plus className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => { setShowHistory(!showHistory); if (!showHistory) loadHistory(); }}
                      className={cn("rounded-lg p-1.5", showHistory ? "text-primary" : "text-muted-foreground hover:text-primary")}
                      title="历史对话"
                    >
                      <History className="h-4 w-4" />
                    </button>
                  </>
                )}
                <button onClick={close} className="text-muted-foreground hover:text-foreground">
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            {!configured ? (
              <div className="flex-1 space-y-4 overflow-auto p-4 text-sm">
                <div className="rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-muted-foreground">
                  分析结论由你自己配置的 AI 给出，本产品只负责把本页数据打包成上下文、并让 AI 能调数据工具，
                  <b className="text-foreground">不校准、不背书、不对结果负责</b>。
                </div>
                <div>
                  <p className="mb-1.5 text-xs font-medium text-muted-foreground">将随提问发给 AI 的本页上下文：</p>
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-black/30 p-3 font-mono text-[11px] leading-relaxed text-muted-foreground">
{context}
                  </pre>
                </div>
                <Link to="/settings" className="flex items-center justify-center gap-2 rounded-lg bg-primary/15 px-3 py-2 text-sm font-medium text-primary hover:bg-primary/25">
                  <Settings className="h-4 w-4" /> 先接入你的 AI（订阅 / API）
                </Link>
              </div>
            ) : showHistory ? (
              <div className="flex-1 overflow-auto p-4">
                {historyList.length === 0 ? (
                  <p className="py-8 text-center text-sm text-muted-foreground">还没有历史对话</p>
                ) : (
                  <div className="space-y-2">
                    {historyList.map((h) => (
                      <button
                        key={h.id}
                        onClick={() => restoreConversation(h.id)}
                        className="block w-full rounded-lg border border-border/40 bg-muted/20 p-3 text-left hover:border-primary/40"
                      >
                        <p className="truncate text-sm font-medium">{h.title}</p>
                        <p className="mt-0.5 truncate text-[11px] text-muted-foreground/60">
                          {h.message_count} 条消息 · {new Date(h.updated_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}
                        </p>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <>
                <div ref={scrollRef} className="flex-1 space-y-3 overflow-auto p-4 text-sm">
                  {msgs.length === 0 && (
                    <div className="rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-muted-foreground">
                      AI 可基于本页上下文、并自行调取 A股行情/估值/研报数据作答。结论由你的模型给出，
                      <b className="text-foreground">不构成投资建议</b>。
                    </div>
                  )}
                  {msgs.map((m, i) => (
                    <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                      <div className={cn(
                        "max-w-[85%] rounded-2xl px-3 py-2 leading-relaxed",
                        m.role === "user" ? "bg-primary/20 text-foreground" : "bg-muted/40 text-foreground",
                      )}>
                        {m.tools && m.tools.length > 0 && (
                          <div className="mb-1.5 flex flex-wrap items-center gap-1">
                            <span className="text-[10px] text-muted-foreground/70">数据来源</span>
                            {m.tools.map((t, j) => (
                              <span key={j} className="inline-flex items-center gap-0.5 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                                <Wrench className="h-2.5 w-2.5" /> {TOOL_LABEL[t.name] || t.name}{t.arg ? ` ${t.arg}` : ""}
                              </span>
                            ))}
                          </div>
                        )}
                        <p className="whitespace-pre-wrap">{m.content}</p>
                        {m.role === "assistant" && m.content && !(loading && i === msgs.length - 1) && (
                          <div className="mt-1.5"><SaveNoteButton kind="问AI" title={`问 AI · ${msgs[i - 1]?.content?.slice(0, 24) || "对话"}`} content={m.content} /></div>
                        )}
                      </div>
                    </div>
                  ))}
                  {loading && (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> AI 正在思考 / 调取数据…
                    </div>
                  )}
                  {err && (
                    <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
                      <AlertCircle className="h-3.5 w-3.5 shrink-0" /> {err}
                    </div>
                  )}
                  {msgs.length === 0 && suggestions.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 pt-1">
                      {suggestions.map((s) => (
                        <button key={s} onClick={() => send(s)} className="rounded-full border border-border bg-muted/40 px-2.5 py-1 text-xs hover:border-primary/40 hover:text-primary">
                          {s}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <div className="border-t border-border/60 p-3">
                  <div className="flex items-end gap-2">
                    <textarea
                      value={input}
                      onChange={(e) => setInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
                      rows={1}
                      placeholder="就本页内容提问…"
                      className="flex-1 resize-none rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50"
                    />
                    <button onClick={() => send(input)} disabled={loading || !input.trim()}
                      className="rounded-lg bg-primary/15 p-2 text-primary hover:bg-primary/25 disabled:opacity-40">
                      <Send className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              </>
            )}
          </aside>
        </div>
      )}
    </>
  );
}
