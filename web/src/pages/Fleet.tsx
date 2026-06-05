import { useEffect, useState } from "react";
import { api, Fleet as FleetT } from "../api";
import { Card, Pill, Spinner, ErrorBox, Empty } from "../components";

export default function Fleet() {
  const [data, setData] = useState<FleetT | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () => api.fleet().then(setData).catch((e) => setError(String(e)));
    load();
    const t = setInterval(load, 8_000);
    return () => clearInterval(t);
  }, []);

  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;

  const queues = Object.entries(data.queues || {});

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Fleet</h1>

      <div className="grid md:grid-cols-2 gap-4">
        {queues.length === 0 ? (
          <Card title="Job queues">
            <Empty text="No Redis configured — all-in-one mode (no queues)" />
          </Card>
        ) : (
          queues.map(([cloud, q]) => (
            <Card key={cloud} title={`Queue · ${cloud}`}>
              <div className="flex gap-8">
                <div>
                  <div className="text-3xl font-semibold">{q.depth}</div>
                  <div className="text-xs text-zinc-500">stream depth</div>
                </div>
                <div>
                  <div className="text-3xl font-semibold">{q.pending}</div>
                  <div className="text-xs text-zinc-500">delivered, unacked</div>
                </div>
              </div>
            </Card>
          ))
        )}
      </div>

      <Card title="Executors (running investigations)">
        {data.executors.length === 0 ? (
          <Empty text="No running investigations" />
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Worker</th>
                <th className="th">Cloud</th>
                <th className="th">Running</th>
                <th className="th">Heartbeat age</th>
              </tr>
            </thead>
            <tbody>
              {data.executors.map((e) => (
                <tr key={`${e.worker_id}-${e.cloud}`}>
                  <td className="td">{e.worker_id || "—"}</td>
                  <td className="td"><Pill value={e.cloud} /></td>
                  <td className="td">{e.running_jobs}</td>
                  <td className="td">
                    <span className={e.heartbeat_age_s > 180 ? "text-red-400" : "text-emerald-400"}>
                      {e.heartbeat_age_s}s
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Orphaned jobs (stale heartbeat — will be reclaimed)">
        {data.orphaned.length === 0 ? (
          <Empty text="None 🎉" />
        ) : (
          <ul className="text-sm space-y-1">
            {data.orphaned.map((o) => (
              <li key={o.id} className="text-amber-300">
                {o.id} — last worker {o.worker_id || "?"} at step {o.step ?? "?"}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
