import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Incident } from "../api";
import { Card, Pill, Ago, Spinner, ErrorBox, Empty } from "../components";

export default function Incidents() {
  const [rows, setRows] = useState<Incident[] | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");

  const search = () =>
    api.incidents(q || undefined).then(setRows).catch((e) => setError(String(e)));
  useEffect(() => { search(); }, []);

  if (error) return <ErrorBox error={error} />;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Incident history</h1>
      <form
        onSubmit={(e) => { e.preventDefault(); search(); }}
        className="flex gap-2"
      >
        <input
          className="input max-w-md"
          placeholder="Search (text + semantic when configured)…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button className="btn-primary" type="submit">Search</button>
      </form>
      {!rows ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <Card><Empty text="No incidents found" /></Card>
      ) : (
        <Card>
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Title</th>
                <th className="th">Source</th>
                <th className="th">Severity</th>
                <th className="th">Status</th>
                <th className="th">When</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="hover:bg-zinc-900/60">
                  <td className="td">
                    <Link className="text-indigo-300 hover:underline" to={`/incidents/${r.id}`}>
                      {r.title}
                    </Link>
                    <div className="text-xs text-zinc-500 line-clamp-1">{r.analysis}</div>
                  </td>
                  <td className="td text-zinc-400">{r.source}</td>
                  <td className="td"><Pill value={r.severity} /></td>
                  <td className="td"><Pill value={r.status} /></td>
                  <td className="td text-zinc-400"><Ago ts={r.created_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
