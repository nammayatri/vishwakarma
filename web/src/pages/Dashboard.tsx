import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Overview } from "../api";
import { Card, Pill, Ago, Spinner, ErrorBox, Empty } from "../components";

export default function Dashboard() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  const load = () => api.overview().then(setData).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, []);

  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;

  const stats = data.incident_stats;
  const queues = data.fleet?.queues || {};

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Dashboard</h1>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card title="Total incidents">
          <div className="text-3xl font-semibold">{stats.total}</div>
        </Card>
        <Card title="Active investigations">
          <div className="text-3xl font-semibold">{data.active_investigations.length}</div>
        </Card>
        <Card title="Queue · aws">
          <div className="text-3xl font-semibold">
            {queues.aws ? queues.aws.depth : "—"}
            <span className="text-sm text-zinc-500 ml-2">
              {queues.aws ? `${queues.aws.pending} pending` : "no redis"}
            </span>
          </div>
        </Card>
        <Card title="Queue · gcp">
          <div className="text-3xl font-semibold">
            {queues.gcp ? queues.gcp.depth : "—"}
            <span className="text-sm text-zinc-500 ml-2">
              {queues.gcp ? `${queues.gcp.pending} pending` : "no redis"}
            </span>
          </div>
        </Card>
      </div>

      <Card title="Live investigations">
        {data.active_investigations.length === 0 ? (
          <Empty text="No investigations in flight" />
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Alert</th>
                <th className="th">Cloud</th>
                <th className="th">Status</th>
                <th className="th">Step</th>
                <th className="th">Worker</th>
                <th className="th">Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.active_investigations.map((i) => (
                <tr key={i.id} className="hover:bg-zinc-900/60">
                  <td className="td">
                    <Link className="text-indigo-300 hover:underline" to={`/investigations/${i.id}`}>
                      {i.alert_key || i.id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="td"><Pill value={i.cloud} /></td>
                  <td className="td"><Pill value={i.status} /></td>
                  <td className="td">{i.step ?? 0}</td>
                  <td className="td text-zinc-400">{i.worker_id || "—"}</td>
                  <td className="td text-zinc-400"><Ago ts={i.updated_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div className="grid md:grid-cols-2 gap-4">
        <Card title="Incidents by status">
          {Object.entries(stats.by_status || {}).map(([k, v]) => (
            <div key={k} className="flex justify-between py-1 text-sm">
              <Pill value={k} /> <span>{v}</span>
            </div>
          ))}
        </Card>
        <Card title="Incidents by source">
          {Object.entries(stats.by_source || {}).map(([k, v]) => (
            <div key={k} className="flex justify-between py-1 text-sm">
              <span className="text-zinc-400">{k || "unknown"}</span> <span>{v}</span>
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
