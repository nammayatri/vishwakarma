import { useState } from "react";
import { api, getToken, setToken } from "../api";
import { Card } from "../components";

export default function Settings() {
  const [token, setTok] = useState(getToken());
  const [check, setCheck] = useState("");

  const save = async () => {
    setToken(token.trim());
    try {
      await api.overview();
      setCheck("Token works ✅");
    } catch (e) {
      setCheck(`Check failed: ${String(e).slice(0, 120)}`);
    }
  };

  return (
    <div className="space-y-4 max-w-xl">
      <h1 className="text-lg font-semibold">Settings</h1>

      <Card title="Access token">
        <p className="text-sm text-zinc-400 mb-3">
          When RBAC is enabled on the server (ui.auth_disabled: false), requests
          need an admin or reader token. Stored in this browser only. Google SSO
          replaces this at deployment.
        </p>
        <div className="flex gap-2">
          <input className="input" type="password" placeholder="X-VK-Token"
                 value={token} onChange={(e) => setTok(e.target.value)} />
          <button className="btn-primary" onClick={save}>Save & test</button>
        </div>
        {check && <div className="text-sm text-zinc-300 mt-2">{check}</div>}
      </Card>

      <Card title="Knowledge & learnings">
        <p className="text-sm text-zinc-400">
          Site knowledge and learnings are managed via the existing API
          (<code className="text-zinc-300">/api/learnings</code>) and the
          PVC-mounted files. A dedicated editor lands with the per-cloud
          knowledge split (Phase 4 deployment).
        </p>
      </Card>

      <Card title="About">
        <p className="text-sm text-zinc-400">
          Argus console — part of the Vishwakarma RCA agent.
          Live investigations stream over SSE from all executor pods via the
          Redis event bus; everything else reads the shared control-plane DB.
        </p>
      </Card>
    </div>
  );
}
