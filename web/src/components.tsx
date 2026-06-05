import { ReactNode } from "react";

export function Card(props: { title?: string; children: ReactNode; className?: string }) {
  return (
    <div className={`card ${props.className || ""}`}>
      {props.title && (
        <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">{props.title}</div>
      )}
      {props.children}
    </div>
  );
}

const STATUS_COLORS: Record<string, string> = {
  running: "bg-blue-900/60 text-blue-200 border-blue-700",
  queued: "bg-zinc-800 text-zinc-300 border-zinc-600",
  done: "bg-emerald-900/60 text-emerald-200 border-emerald-700",
  failed: "bg-red-900/60 text-red-200 border-red-700",
  awaiting_fix_review: "bg-amber-900/60 text-amber-200 border-amber-700",
  open: "bg-blue-900/60 text-blue-200 border-blue-700",
  resolved: "bg-emerald-900/60 text-emerald-200 border-emerald-700",
  critical: "bg-red-900/60 text-red-200 border-red-700",
  high: "bg-amber-900/60 text-amber-200 border-amber-700",
  warning: "bg-yellow-900/60 text-yellow-200 border-yellow-700",
  active: "bg-emerald-900/60 text-emerald-200 border-emerald-700",
  demoted: "bg-red-900/60 text-red-200 border-red-700",
  aws: "bg-orange-900/60 text-orange-200 border-orange-700",
  gcp: "bg-sky-900/60 text-sky-200 border-sky-700",
  both: "bg-purple-900/60 text-purple-200 border-purple-700",
};

export function Pill({ value }: { value?: string }) {
  if (!value) return null;
  const color = STATUS_COLORS[value] || "bg-zinc-800 text-zinc-300 border-zinc-600";
  return (
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs border ${color}`}>
      {value}
    </span>
  );
}

export function Ago({ ts }: { ts?: number }) {
  if (!ts) return <span className="text-zinc-600">—</span>;
  const s = Math.max(0, Date.now() / 1000 - ts);
  const txt =
    s < 60 ? `${Math.round(s)}s ago`
    : s < 3600 ? `${Math.round(s / 60)}m ago`
    : s < 86400 ? `${Math.round(s / 3600)}h ago`
    : `${Math.round(s / 86400)}d ago`;
  return <span title={new Date(ts * 1000).toLocaleString()}>{txt}</span>;
}

export function Spinner() {
  return (
    <div className="flex justify-center p-8">
      <div className="w-6 h-6 border-2 border-zinc-700 border-t-indigo-500 rounded-full animate-spin" />
    </div>
  );
}

export function ErrorBox({ error }: { error: string }) {
  return (
    <div className="card border-red-800 bg-red-950/40 text-red-200 text-sm">{error}</div>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="text-zinc-600 text-sm p-6 text-center">{text}</div>;
}
