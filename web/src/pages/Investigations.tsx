import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Investigation } from "../api";
import { Pill, Ago, Spinner, ErrorBox, Empty, Card } from "../components";

const FILTERS = ["", "running", "queued", "done", "failed", "awaiting_fix_review"];

export default function Investigations() {
  const [rows, setRows] = useState<Investigation[] | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  const load = () =>
    api.investigations(filter || undefined).then(setRows).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
    const t = setInterval(load, 8_000);
    return () => clearInterval(t);
  }, [filter]);

  if (error) return <ErrorBox error={error} />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Investigations</h1>
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`btn text-xs ${filter === f ? "border-indigo-500 text-indigo-300" : ""}`}
            >
              {f || "all"}
            </button>
          ))}
        </div>
      </div>
      {!rows ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <Card><Empty text="No investigations" /></Card>
      ) : (
        <Card>
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Alert</th>
                <th className="th">Cloud</th>
                <th className="th">Status</th>
                <th className="th">Phase</th>
                <th className="th">Step</th>
                <th className="th">Attempt</th>
                <th className="th">Worker</th>
                <th className="th">Updated</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.id} className="hover:bg-zinc-900/60">
                  <td className="td">
                    <Link className="text-indigo-300 hover:underline" to={`/investigations/${i.id}`}>
                      {i.alert_key || i.id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="td"><Pill value={i.cloud} /></td>
                  <td className="td"><Pill value={i.status} /></td>
                  <td className="td text-zinc-400">{i.phase || "—"}</td>
                  <td className="td">{i.step ?? 0}</td>
                  <td className="td">{i.attempt ?? 0}</td>
                  <td className="td text-zinc-400">{i.worker_id || "—"}</td>
                  <td className="td text-zinc-400"><Ago ts={i.updated_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
