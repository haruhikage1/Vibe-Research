import { useState, useEffect } from "react";
import { Trash2, ChevronDown, ChevronRight, NotebookPen, MessageSquare, Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { Disclaimer } from "@/components/ui/Disclaimer";
import { loadNotes, deleteNote, clearNotes, type Note } from "@/lib/notes";
import { api, ApiError, type ConversationListItem, type Conversation } from "@/lib/api";
import { cn } from "@/lib/utils";

const KIND_COLOR: Record<string, string> = {
  复盘: "bg-primary/15 text-primary",
  今日要点: "bg-warning/15 text-warning",
  问AI: "bg-success/15 text-success",
};

const PAGE_LABEL: Record<string, string> = {
  "daily-review": "每日复盘",
  "stock-data": "个股数据",
  watchlist: "自选股",
  portfolio: "我的持仓",
  "sector-detail": "板块中心",
  intel: "资讯雷达",
};

export function Notes() {
  const [tab, setTab] = useState<"notes" | "conversations">("notes");
  const [notes, setNotes] = useState<Note[]>(loadNotes);
  const [openId, setOpenId] = useState<string | null>(null);

  const [convList, setConvList] = useState<ConversationListItem[]>([]);
  const [openConv, setOpenConv] = useState<Conversation | null>(null);
  const [convLoading, setConvLoading] = useState(false);
  const [convErr, setConvErr] = useState<string | null>(null);

  const fmt = (ts: number) => new Date(ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

  const loadConversations = async () => {
    setConvLoading(true);
    try {
      setConvList(await api.conversations());
    } catch (e) {
      setConvErr(e instanceof ApiError ? e.message : "加载对话列表失败");
    } finally {
      setConvLoading(false);
    }
  };

  useEffect(() => {
    if (tab === "conversations" && convList.length === 0) loadConversations();
  }, [tab]);

  const openConversation = async (id: string) => {
    try {
      const conv = await api.conversation(id);
      setOpenConv(openConv?.id === id ? null : conv);
    } catch (e) {
      setConvErr(e instanceof ApiError ? e.message : "加载对话失败");
    }
  };

  const deleteConversation = async (id: string) => {
    if (!confirm("删除这条对话？")) return;
    try {
      await api.deleteConversation(id);
      setConvList(convList.filter((c) => c.id !== id));
      if (openConv?.id === id) setOpenConv(null);
    } catch (e) {
      setConvErr(e instanceof ApiError ? e.message : "删除失败");
    }
  };

  const clearAllConversations = async () => {
    if (!confirm("清空所有对话记录？")) return;
    try {
      await api.clearConversations();
      setConvList([]);
      setOpenConv(null);
    } catch (e) {
      setConvErr(e instanceof ApiError ? e.message : "清空失败");
    }
  };

  return (
    <div>
      <PageHeader
        title="研究记录"
        subtitle="把 AI 复盘 / 要点 / 问答沉淀在本地，随时回看。数据只存本地、不上传。"
        actions={
          tab === "notes" ? (
            notes.length > 0 && (
              <button onClick={() => { if (confirm("清空所有研究记录？")) { clearNotes(); setNotes([]); } }}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-muted-foreground hover:text-destructive">
                <Trash2 className="h-4 w-4" /> 清空
              </button>
            )
          ) : (
            convList.length > 0 && (
              <button onClick={clearAllConversations}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-muted-foreground hover:text-destructive">
                <Trash2 className="h-4 w-4" /> 清空
              </button>
            )
          )
        }
      />

      <div className="mb-4 flex gap-2">
        <button
          onClick={() => setTab("notes")}
          className={cn("inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
            tab === "notes" ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground")}
        >
          <NotebookPen className="h-4 w-4" /> 沉淀笔记
        </button>
        <button
          onClick={() => setTab("conversations")}
          className={cn("inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
            tab === "conversations" ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground")}
        >
          <MessageSquare className="h-4 w-4" /> 对话记录
        </button>
      </div>

      {tab === "notes" ? (
        notes.length === 0 ? (
          <GlassCard>
            <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
              <NotebookPen className="h-8 w-8 text-muted-foreground/40" />
              还没有记录。在「每日复盘」「资讯雷达」或「问 AI」里点 <b className="text-foreground">「存入沉淀」</b> 保存分析结果。
            </div>
          </GlassCard>
        ) : (
          <div className="space-y-2">
            {notes.map((n) => {
              const open = openId === n.id;
              return (
                <GlassCard key={n.id} className="!p-0 overflow-hidden">
                  <div className="flex items-center gap-2 px-4 py-3">
                    <button onClick={() => setOpenId(open ? null : n.id)} className="flex flex-1 items-center gap-2 text-left">
                      {open ? <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />}
                      <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${KIND_COLOR[n.kind] || "bg-muted/50 text-muted-foreground"}`}>{n.kind}</span>
                      <span className="flex-1 truncate text-sm font-medium">{n.title}</span>
                      <span className="shrink-0 font-mono text-[11px] text-muted-foreground/60">{fmt(n.ts)}</span>
                    </button>
                    <button onClick={() => setNotes(deleteNote(n.id))} className="shrink-0 text-muted-foreground/60 hover:text-destructive" title="删除">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  {open && (
                    <div className="border-t border-border/40 px-4 py-3">
                      <div className="prose prose-sm prose-invert max-w-none text-foreground">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{n.content}</ReactMarkdown>
                      </div>
                    </div>
                  )}
                </GlassCard>
              );
            })}
          </div>
        )
      ) : (
        <>
          {convErr && (
            <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
              {convErr}
            </div>
          )}
          {convLoading ? (
            <GlassCard>
              <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" /> 加载对话记录…
              </div>
            </GlassCard>
          ) : convList.length === 0 ? (
            <GlassCard>
              <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
                <MessageSquare className="h-8 w-8 text-muted-foreground/40" />
                还没有对话记录。在任意页面点「问 AI」开始对话，对话会自动保存在本地。
              </div>
            </GlassCard>
          ) : (
            <div className="space-y-2">
              {convList.map((c) => (
                <GlassCard key={c.id} className="!p-0 overflow-hidden">
                  <div className="flex items-center gap-2 px-4 py-3">
                    <button onClick={() => openConversation(c.id)} className="flex flex-1 items-center gap-2 text-left">
                      {openConv?.id === c.id ? <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />}
                      <span className="shrink-0 rounded-full bg-success/15 px-2 py-0.5 text-[10px] text-success">
                        {PAGE_LABEL[c.page] || c.page}
                      </span>
                      <span className="flex-1 truncate text-sm font-medium">{c.title}</span>
                      <span className="shrink-0 text-[11px] text-muted-foreground/60">{c.message_count} 条</span>
                      <span className="shrink-0 font-mono text-[11px] text-muted-foreground/60">{fmt(c.updated_at)}</span>
                    </button>
                    <button onClick={() => deleteConversation(c.id)} className="shrink-0 text-muted-foreground/60 hover:text-destructive" title="删除">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  {openConv?.id === c.id && (
                    <div className="border-t border-border/40 px-4 py-3 space-y-3">
                      {openConv.messages.map((m, i) => (
                        <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                          <div className={cn(
                            "max-w-[85%] rounded-2xl px-3 py-2 text-sm leading-relaxed",
                            m.role === "user" ? "bg-primary/20 text-foreground" : "bg-muted/40 text-foreground",
                          )}>
                            {m.tools && m.tools.length > 0 && (
                              <div className="mb-1.5 flex flex-wrap items-center gap-1">
                                <span className="text-[10px] text-muted-foreground/70">数据来源</span>
                                {m.tools.map((t, j) => (
                                  <span key={j} className="inline-flex items-center gap-0.5 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                                    {t.tool}{t.args && Object.keys(t.args).length ? ` ${JSON.stringify(t.args).slice(0, 20)}` : ""}
                                  </span>
                                ))}
                              </div>
                            )}
                            <p className="whitespace-pre-wrap">{m.content}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </GlassCard>
              ))}
            </div>
          )}
        </>
      )}

      <Disclaimer />
    </div>
  );
}
