import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ArtifactEvidence } from "../api";
import EvidenceDrawer from "../components/harness/EvidenceDrawer";
import { useNav } from "../App";
import { useScrollContainment } from "../useScrollContainment";
import AgentComposer from "./AgentComposer";
import AssistantTurnView from "./AssistantTurnView";
import ContextChips from "./ContextChips";
import MessageBubble from "./MessageBubble";
import {
  emptyTurnState,
  mergeTurnState,
  reduceEvents,
  type AssistantTranscriptItem,
  type TranscriptItem,
} from "./transcript";
import type { ContextEnvelope, UiIntent } from "./types";
import { applyUiIntent } from "./uiBridge";
import { useAgentStream } from "./useAgentStream";

let _uidSeq = 0;
function uid(prefix: string): string {
  _uidSeq += 1;
  return `${prefix}-${Date.now().toString(36)}-${_uidSeq}`;
}

function convKey(projectId?: string | null): string {
  return `manju:agent:conv:${projectId ?? "global"}`;
}

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (content && typeof content === "object") {
    const obj = content as Record<string, unknown>;
    if (typeof obj.text === "string") return obj.text;
    if (typeof obj.reply === "string") return obj.reply;
    return JSON.stringify(content);
  }
  return content == null ? "" : String(content);
}

interface ApiMessage {
  id: string;
  role: string;
  content: unknown;
  turn_id?: string | null;
  created_at: number;
}

function mapHistory(msgs: ApiMessage[]): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  for (const m of msgs) {
    if (m.role === "user") {
      items.push({
        kind: "user",
        id: m.id,
        text: contentToText(m.content),
        createdAt: m.created_at,
      });
    } else if (m.role === "assistant") {
      items.push({
        kind: "assistant",
        id: m.id,
        turnId: m.turn_id ?? null,
        ...emptyTurnState(),
        status: "done",
        answer: contentToText(m.content),
        createdAt: m.created_at,
      });
    }
  }
  return items;
}

export default function AgentDrawer({
  open,
  onClose,
  context,
}: {
  open: boolean;
  onClose: () => void;
  context: ContextEnvelope;
}) {
  const { go, toast } = useNav();
  const drawerRef = useRef<HTMLElement | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const conversationScope = convKey(context.project_id);
  const scopeRef = useRef(conversationScope);
  useScrollContainment(drawerRef, open);

  const [conversationId, setConversationId] = useState<string | null>(null);
  const [turnId, setTurnId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [messages, setMessages] = useState<TranscriptItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const streaming = Boolean(turnId) && sending;
  const {
    events,
    streamTurnId,
    status: streamStatus,
    reset: resetStream,
  } = useAgentStream(turnId, Boolean(turnId));
  const turnState = useMemo(() => reduceEvents(events), [events]);

  // 切换项目后立即切换会话作用域，避免把新项目的消息发进旧项目会话。
  useEffect(() => {
    if (scopeRef.current === conversationScope) return;
    scopeRef.current = conversationScope;
    resetStream();
    setConversationId(null);
    setTurnId(null);
    setSending(false);
    setMessages([]);
    setError(null);
  }, [conversationScope, resetStream]);

  // Esc 收起
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 会话引导：优先复用本机上次会话（含历史回填），失败或不存在则新建。
  useEffect(() => {
    if (!open || conversationId) return;
    let cancelled = false;
    const key = conversationScope;

    const createNew = async () => {
      const conv = (await api.post("/agent/conversations", {
        project_id: context.project_id ?? null,
        title: context.project_id
          ? `项目 ${context.project_id.slice(0, 8)}`
          : "案头助手",
      })) as { id: string };
      if (cancelled) return;
      try {
        localStorage.setItem(key, conv.id);
      } catch {
        /* 隐私模式忽略 */
      }
      setConversationId(conv.id);
      setMessages([]);
    };

    (async () => {
      let stored: string | null = null;
      try {
        stored = localStorage.getItem(key);
      } catch {
        stored = null;
      }
      if (stored) {
        try {
          const data = (await api.get(`/agent/conversations/${stored}`)) as {
            conversation: { id: string };
            messages: ApiMessage[];
          };
          if (cancelled) return;
          setConversationId(data.conversation.id);
          setMessages(mapHistory(data.messages || []));
          return;
        } catch {
          /* 会话已不存在（如 DB 重置）→ 落到新建 */
        }
      }
      try {
        await createNew();
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, conversationId, context.project_id, conversationScope]);

  // 把当前 turn 的事件流折叠进对应的 assistant 消息（不覆盖历史，其它消息原样保留）。
  useEffect(() => {
    if (!turnId || streamTurnId !== turnId || events.length === 0) return;
    setMessages((prev) =>
      mergeTurnState(prev, turnId, streamTurnId, events.length, turnState),
    );
    if (turnState.status !== "streaming") setSending(false);
  }, [events.length, streamTurnId, turnState, turnId]);

  // 贴底滚动：仅当用户本就在底部附近时才自动跟随，避免打断向上翻阅。
  const onTranscriptScroll = useCallback(() => {
    const el = transcriptRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);
  useEffect(() => {
    const el = transcriptRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!conversationId || !text || sending) return;
    setSending(true);
    setError(null);
    const userItem: TranscriptItem = {
      kind: "user",
      id: uid("u"),
      text,
      createdAt: Date.now() / 1000,
    };
    const assistantId = uid("a");
    const assistantItem: AssistantTranscriptItem = {
      kind: "assistant",
      id: assistantId,
      turnId: null,
      ...emptyTurnState(),
      createdAt: Date.now() / 1000,
    };
    setMessages((prev) => [...prev, userItem, assistantItem]);
    setInput("");
    stickRef.current = true;
    // 先解除上一轮关联，再清空 SSE；否则空事件会被写回上一轮。
    setTurnId(null);
    resetStream();
    try {
      const resp = (await api.post(
        `/agent/conversations/${conversationId}/messages`,
        {
          content: text,
          context,
        },
      )) as { turn_id: string };
      setMessages((prev) =>
        prev.map((m) =>
          m.kind === "assistant" && m.id === assistantId
            ? { ...m, turnId: resp.turn_id }
            : m,
        ),
      );
      setTurnId(resp.turn_id);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setMessages((prev) =>
        prev.map((m) =>
          m.kind === "assistant" && m.id === assistantId
            ? { ...m, status: "failed", error: msg }
            : m,
        ),
      );
      setSending(false);
      setError(msg);
    }
  }, [conversationId, input, sending, context, resetStream]);

  const stop = useCallback(async () => {
    if (!turnId) return;
    try {
      await api.post(`/agent/turns/${turnId}/cancel`, { cancel_run: false });
      toast("已停止本轮对话（底层 Run 未取消）");
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), true);
    } finally {
      setSending(false);
    }
  }, [turnId, toast]);

  const approve = useCallback(
    async (toolCallId: string, reason: string) => {
      try {
        await api.post(`/agent/tool-calls/${toolCallId}/approve`, { reason });
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err), true);
      }
    },
    [toast],
  );

  const reject = useCallback(
    async (toolCallId: string, reason: string) => {
      try {
        await api.post(`/agent/tool-calls/${toolCallId}/reject`, { reason });
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err), true);
      }
    },
    [toast],
  );

  const [evidence, setEvidence] = useState<ArtifactEvidence | null>(null);

  const openEvidence = useCallback(
    async (artifactId: string) => {
      try {
        const art = (await api.get(
          `/artifacts/${encodeURIComponent(artifactId)}`,
        )) as ArtifactEvidence;
        setEvidence(art);
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err), true);
      }
    },
    [toast],
  );

  const followIntent = useCallback(
    (intent: UiIntent | null) => {
      if (!intent) return;
      const result = applyUiIntent(intent, go, {
        toast,
        onOpenEvidence: (id) => {
          void openEvidence(id);
        },
        onSelectShot: (episodeId, shotId) => {
          try {
            sessionStorage.setItem(
              "manju:select_shot",
              JSON.stringify({ episodeId, shotId }),
            );
          } catch {
            /* ignore quota */
          }
          go("wall", undefined, episodeId);
        },
        onOpenCredentials: () => go("monitor"),
      });
      if (!result.ok) toast(result.message || "定位失败", true);
    },
    [go, toast, openEvidence],
  );

  const activeTurn = useMemo(
    () =>
      messages.find(
        (m): m is AssistantTranscriptItem =>
          m.kind === "assistant" && m.turnId === turnId,
      ),
    [messages, turnId],
  );

  const statusLabel = useMemo(() => {
    if (activeTurn && activeTurn.approvals.length > 0) return "待批准";
    if (sending) return streamStatus === "connecting" ? "连接中…" : "生成中…";
    return "";
  }, [activeTurn, sending, streamStatus]);

  return (
    <aside
      id="agent-drawer"
      ref={drawerRef}
      className={`agent-drawer ${open ? "open" : ""}`}
      aria-label="案头助手"
      aria-hidden={!open}
    >
      <div className="agent-drawer-head">
        <div>
          <b>案头助手</b>
          {statusLabel && (
            <span className={`agent-stream-status ${sending ? "live" : ""}`}>
              {sending && (
                <span className="agent-status-dot" aria-hidden="true" />
              )}
              {statusLabel}
            </span>
          )}
        </div>
        <button
          type="button"
          className="agent-panel-toggle"
          aria-label="收起案头助手"
          title="收起案头助手"
          onClick={onClose}
        >
          <svg
            className="agent-toggle-icon"
            viewBox="0 0 22 18"
            aria-hidden="true"
            focusable="false"
          >
            <rect
              x="1.25"
              y="1.25"
              width="19.5"
              height="15.5"
              rx="2.2"
              ry="2.2"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
            <path
              d="M6.75 1.25v15.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          </svg>
        </button>
      </div>

      <ContextChips context={context} />

      <div
        className="agent-transcript"
        ref={transcriptRef}
        onScroll={onTranscriptScroll}
      >
        {error && (
          <div className="agent-error" role="alert">
            {error}
          </div>
        )}

        {messages.length === 0 && (
          <div className="agent-empty">
            <p>
              我是案头助手：默认先帮你诊断状态并指路到对应工作台；付费/破坏性写入会请你确认，也可直接在页面按钮完成。
            </p>
          </div>
        )}

        {messages.map((item) =>
          item.kind === "user" ? (
            <MessageBubble key={item.id} text={item.text} />
          ) : (
            <AssistantTurnView
              key={item.id}
              item={item}
              onApprove={approve}
              onReject={reject}
              onOpenRun={(runId) => {
                go("monitor");
                const params = new URLSearchParams({
                  section: "runs",
                  run_id: runId,
                  focus: String(Date.now()),
                });
                window.history.replaceState({}, "", `/monitor?${params}`);
                window.dispatchEvent(new PopStateEvent("popstate"));
              }}
              onOpenEvidence={(id) => {
                void openEvidence(id);
              }}
              onFollowIntent={() => followIntent(item.intent)}
            />
          ),
        )}
      </div>

      <AgentComposer
        value={input}
        onChange={setInput}
        onSend={send}
        onStop={stop}
        disabled={sending || !conversationId}
        stopping={streaming}
      />
      {evidence && (
        <div className="agent-evidence-host" style={{ padding: "0 12px 12px" }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: 6,
            }}
          >
            <span className="hint">证据 {evidence.id}</span>
            <button
              type="button"
              className="btn small ghost"
              onClick={() => setEvidence(null)}
            >
              关闭
            </button>
          </div>
          <EvidenceDrawer evidence={evidence} label="打开证据抽屉" />
        </div>
      )}
    </aside>
  );
}
