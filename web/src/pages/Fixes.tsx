import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Investigation } from "../api";
import { Card, Pill, Ago, Spinner, ErrorBox, Empty } from "../components";

export default function Fixes() {
  const [rows, setRows] = useState<Investigation[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () => api.fixes().then(setRows).catch((e) => setError(String(e)));
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, []);

  if (error) return <ErrorBox error={error} />;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Fixes & PRs</h1>
      <div className="text-sm text-zinc-500">
        Investigations whose fix awaits review. Draft-PR creation lands with the
        GitHub App (Phase 3 completion) — approval flips the PR draft→ready,
        merge stays on GitHub.
      </div>
      {!rows ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <Card><Empty text="No fixes awaiting review" /></Card>
      ) : (
        <Card>
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Alert</th>
                <th className="th">Cloud</th>
                <th className="th">Code session</th>
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
                  <td className="td text-zinc-400">{i.code_session ? "yes" : "—"}</td>
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
