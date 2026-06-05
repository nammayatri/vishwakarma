import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Incident } from "../api";
import { Card, Pill, Ago, Spinner, ErrorBox } from "../components";

export default function IncidentDetail() {
  const { id } = useParams<{ id: string }>();
  const [inc, setInc] = useState<Incident | null>(null);
  const [error, setError] = useState("");
  const [fb, setFb] = useState("");

  useEffect(() => {
    if (id) api.incident(id).then(setInc).catch((e) => setError(String(e)));
  }, [id]);

  const sendFeedback = async (correct: boolean) => {
    if (!id) return;
    try {
      await api.feedback(id, correct, [], inc?.title || "");
      setFb(correct ? "Marked correct ✅ — learning loops updated" : "Marked wrong ❌ — counters updated");
    } catch (e) {
      setFb(String(e));
    }
  };

  if (error) return <ErrorBox error={error} />;
  if (!inc) return <Spinner />;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">{inc.title}</h1>
        <Pill value={inc.severity} />
        <Pill value={inc.status} />
        <span className="text-sm text-zinc-500">
          {inc.source} · <Ago ts={inc.created_at} />
        </span>
      </div>

      <div className="flex gap-2 items-center">
        <button className="btn" onClick={() => sendFeedback(true)}>✅ RCA correct</button>
        <button className="btn" onClick={() => sendFeedback(false)}>❌ RCA wrong</button>
        {fb && <span className="text-sm text-zinc-400">{fb}</span>}
      </div>

      <Card title="Question">
        <pre className="text-sm whitespace-pre-wrap text-zinc-300">{inc.question}</pre>
      </Card>

      <Card title="Analysis (RCA)">
        <pre className="text-sm whitespace-pre-wrap text-zinc-200">{inc.analysis}</pre>
      </Card>

      {inc.labels && Object.keys(inc.labels).length > 0 && (
        <Card title="Labels">
          <div className="flex flex-wrap gap-2">
            {Object.entries(inc.labels).map(([k, v]) => (
              <span key={k} className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1">
                {k}={String(v)}
              </span>
            ))}
          </div>
        </Card>
      )}

      {Array.isArray(inc.tool_outputs) && inc.tool_outputs.length > 0 && (
        <Card title={`Tool outputs (${inc.tool_outputs.length})`}>
          <pre className="text-xs whitespace-pre-wrap text-zinc-400 max-h-96 overflow-y-auto">
            {JSON.stringify(inc.tool_outputs, null, 1).slice(0, 20000)}
          </pre>
        </Card>
      )}
    </div>
  );
}
