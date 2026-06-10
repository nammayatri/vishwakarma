import { NavLink, Outlet } from "react-router-dom";

const NAV = [
  { to: "/", label: "Dashboard", exact: true },
  { to: "/investigations", label: "Investigations" },
  { to: "/incidents", label: "Incidents" },
  { to: "/runbooks", label: "Runbooks" },
  { to: "/fixes", label: "Fixes & PRs" },
  { to: "/fleet", label: "Fleet" },
  { to: "/settings", label: "Settings" },
];

export default function App() {
  return (
    <div className="min-h-screen flex">
      <aside className="w-52 shrink-0 border-r border-zinc-800 p-4 flex flex-col gap-1">
        <div className="flex items-center gap-2 mb-6 px-2">
          <span className="text-xl">👁</span>
          <span className="font-semibold tracking-wide">Argus</span>
          <span className="text-xs text-zinc-500 mt-1">console</span>
        </div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.exact}
            className={({ isActive }) =>
              `px-3 py-2 rounded-lg text-sm transition-colors ${
                isActive
                  ? "bg-indigo-600/20 text-indigo-300 border border-indigo-800"
                  : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
              }`
            }
          >
            {n.label}
          </NavLink>
        ))}
        <div className="mt-auto text-xs text-zinc-600 px-2">
          vishwakarma · argus branch
        </div>
      </aside>
      <main className="flex-1 p-6 overflow-x-auto">
        <Outlet />
      </main>
    </div>
  );
}
