import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Investigation, subscribeEvents } from "../api";
import { Card, Pill, Ago, Spinner, ErrorBox } from "../components";

interface LiveEvent {
  incident_id?: string;
  type?: string;
  tool?: string;
  status?: string;
  content?: string;
  message?: string;
  [k: string]: unknown;
}

function eventLine(e: LiveEvent): string {
  switch (e.type) {
    case "investigation_started": return `🚨 started: ${e.title}`;
    case "tool_call_start": return `⚙ ${e.tool}(…)`;
    case "tool_call_result": return `   ↳ ${e.tool}: ${e.status}`;
    case "hypothesis": return `💭 ${String(e.content || "").slice(0, 160)}`;
    case "compaction": return "🗜 context compacted";
    case "status": return `· ${e.message}`;
    case "done": return "✅ investigation complete";
    case "max_steps_reached": return "⏹ max steps reached";
    default: return e.type || "event";
  }
}

export default function InvestigationDetail() {
  const { id } = useParams<{ id: string }>();
  const [inv, setInv] = useState<Investigation | null>(null);
  const [error, setError] = useState("");
  const [live, setLive] = useState<LiveEvent[]>([]);
  const [showTranscript, setShowTranscript] = useState(false);
  const liveEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!id) return;
    const load = () => api.investigation(id).then(setInv).catch((e) => setError(String(e)));
    load();
    const t = setInterval(load, 10_000);
    const stop = subscribeEvents((evt) => {
      setLive((prev) => [...prev.slice(-300), evt as LiveEvent]);
    }, id);
    return () => { clearInterval(t); stop(); };
  }, [id]);

  useEffect(() => {
    liveEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [live]);

  if (error) return <ErrorBox error={error} />;
  if (!inv) return <Spinner />;

  const msgs = inv.messages || [];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">{inv.alert_key || inv.id}</h1>
        <Pill value={inv.cloud} />
        <Pill value={inv.status} />
        <span className="text-sm text-zinc-500">
          step {inv.step ?? 0} · attempt {inv.attempt ?? 0} · worker {inv.worker_id || "—"} ·{" "}
          <Ago ts={inv.updated_at} />
        </span>
      </div>

      <Card title="Live events (SSE)">
        <div className="font-mono text-xs space-y-1 max-h-72 overflow-y-auto">
          {live.length === 0 && (
            <div className="text-zinc-600">Waiting for events… (live only while running)</div>
          )}
          {live.map((e, i) => (
            <div key={i} className="text-zinc-300">{eventLine(e)}</div>
          ))}
          <div ref={liveEnd} />
        </div>
      </Card>

      <Card title={`Checkpointed conversation (${msgs.length} messages)`}>
        <button className="btn mb-3" onClick={() => setShowTranscript(!showTranscript)}>
          {showTranscript ? "Hide" : "Show"} transcript
        </button>
        {showTranscript && (
          <div className="space-y-2 max-h-[36rem] overflow-y-auto">
            {msgs.map((m: any, i: number) => (
              <div key={i} className="border border-zinc-800 rounded-lg p-2">
                <div className="text-xs text-zinc-500 mb-1">
                  {m.role}
                  {m.tool_calls?.length ? ` · ${m.tool_calls.length} tool call(s)` : ""}
                </div>
                {m.content ? (
                  <pre className="text-xs whitespace-pre-wrap text-zinc-300">
                    {typeof m.content === "string"
                      ? m.content.slice(0, 4000)
                      : JSON.stringify(m.content)?.slice(0, 4000)}
                  </pre>
                ) : null}
                {/* Assistant actions live in tool_calls — render them so the
                    transcript shows what the agent DID, not empty boxes. */}
                {m.tool_calls?.map((tc: any, j: number) => (
                  <pre key={j} className="text-xs whitespace-pre-wrap text-emerald-300">
                    → {tc.function?.name}({(tc.function?.arguments || "").slice(0, 400)})
                  </pre>
                ))}
              </div>
            ))}
          </div>
        )}
      </Card>

      {inv.code_session ? (
        <Card title="Code session">
          <pre className="text-xs whitespace-pre-wrap text-zinc-300">
            {JSON.stringify(inv.code_session, null, 2).slice(0, 6000)}
          </pre>
        </Card>
      ) : null}
    </div>
  );
}
