import { useEffect, useState } from "react";
import { api, Runbook } from "../api";
import { Card, Pill, Spinner, ErrorBox, Empty } from "../components";

export default function Runbooks() {
  const [rows, setRows] = useState<Runbook[] | null>(null);
  const [sel, setSel] = useState<Runbook | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dryQuery, setDryQuery] = useState("");
  const [dryResult, setDryResult] = useState<string>("");

  const load = () => api.runbooks().then(setRows).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);

  const save = async () => {
    if (!sel) return;
    try {
      const saved = await api.saveRunbook(sel.id, {
        title: sel.title, content_md: sel.content_md, cloud_type: sel.cloud_type,
        keywords: sel.keywords, services: sel.services,
      });
      setNotice(`Saved v${saved.version}`);
      load();
    } catch (e) { setNotice(String(e)); }
  };

  const del = async () => {
    if (!sel || !confirm(`Delete runbook '${sel.id}'?`)) return;
    try { await api.deleteRunbook(sel.id); setSel(null); setNotice("Deleted"); load(); }
    catch (e) { setNotice(String(e)); }
  };

  const mapAlert = async () => {
    if (!sel) return;
    const name = prompt("Alert name to map to this runbook:");
    if (!name) return;
    try { await api.mapAlert(sel.id, name); setNotice(`Mapped '${name}'`); }
    catch (e) { setNotice(String(e)); }
  };

  const dryRun = async () => {
    try {
      const r = await api.dryRun(dryQuery);
      setDryResult(r.length ? r.map((m) => `${m.id} (${m.cloud_type})`).join(", ") : "no match");
    } catch (e) { setDryResult(String(e)); }
  };

  const newRunbook = () => {
    const id = prompt("New runbook id (slug, e.g. redis-evictions):");
    if (!id) return;
    setSel({ id, title: "", content_md: "## Steps\n1. ", cloud_type: "any",
             keywords: [], services: [], version: 0, status: "active",
             hit_count: 0, miss_count: 0 });
  };

  if (error) return <ErrorBox error={error} />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Runbook studio</h1>
        <button className="btn-primary" onClick={newRunbook}>+ New runbook</button>
      </div>

      <Card title="Dry-run: what would match this alert?">
        <div className="flex gap-2">
          <input className="input max-w-lg" placeholder="e.g. RDS-CPU-Production-High"
                 value={dryQuery} onChange={(e) => setDryQuery(e.target.value)} />
          <button className="btn" onClick={dryRun}>Test</button>
          {dryResult && <span className="text-sm text-zinc-300 self-center">{dryResult}</span>}
        </div>
      </Card>

      <div className="grid md:grid-cols-[20rem_1fr] gap-4">
        <Card title={`Runbooks (${rows?.length ?? "…"})`}>
          {!rows ? <Spinner /> : rows.length === 0 ? <Empty text="None yet" /> : (
            <div className="space-y-1 max-h-[32rem] overflow-y-auto">
              {rows.map((r) => (
                <button key={r.id}
                  onClick={() => { setSel({ ...r }); setNotice(""); }}
                  className={`w-full text-left px-3 py-2 rounded-lg text-sm hover:bg-zinc-900 ${
                    sel?.id === r.id ? "bg-indigo-600/20 border border-indigo-800" : ""}`}>
                  <div className="flex items-center justify-between">
                    <span className="truncate">{r.id}</span>
                    <Pill value={r.cloud_type} />
                  </div>
                  <div className="text-xs text-zinc-500">
                    v{r.version} · ✅{r.hit_count} ❌{r.miss_count}
                  </div>
                </button>
              ))}
            </div>
          )}
        </Card>

        <Card title={sel ? `Edit: ${sel.id}` : "Select a runbook"}>
          {!sel ? <Empty text="Pick a runbook on the left, or create one" /> : (
            <div className="space-y-3">
              <input className="input" placeholder="Title" value={sel.title}
                     onChange={(e) => setSel({ ...sel, title: e.target.value })} />
              <div className="flex gap-2">
                <select className="input max-w-32" value={sel.cloud_type}
                        onChange={(e) => setSel({ ...sel, cloud_type: e.target.value })}>
                  {["any", "aws", "gcp", "both"].map((c) => <option key={c}>{c}</option>)}
                </select>
                <input className="input" placeholder="keywords, comma separated"
                       value={sel.keywords.join(", ")}
                       onChange={(e) => setSel({ ...sel,
                         keywords: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
              </div>
              <textarea className="input font-mono h-80" value={sel.content_md}
                        onChange={(e) => setSel({ ...sel, content_md: e.target.value })} />
              <div className="flex gap-2 items-center">
                <button className="btn-primary" onClick={save}>Save</button>
                <button className="btn" onClick={mapAlert}>Map alert →</button>
                <button className="btn-danger" onClick={del}>Delete</button>
                {notice && <span className="text-sm text-zinc-400">{notice}</span>}
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
