import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import App from "./App";
import Dashboard from "./pages/Dashboard";
import Investigations from "./pages/Investigations";
import InvestigationDetail from "./pages/InvestigationDetail";
import Incidents from "./pages/Incidents";
import IncidentDetail from "./pages/IncidentDetail";
import Runbooks from "./pages/Runbooks";
import Fixes from "./pages/Fixes";
import Fleet from "./pages/Fleet";
import Settings from "./pages/Settings";
import "./index.css";

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <App />,
      children: [
        { index: true, element: <Dashboard /> },
        { path: "investigations", element: <Investigations /> },
        { path: "investigations/:id", element: <InvestigationDetail /> },
        { path: "incidents", element: <Incidents /> },
        { path: "incidents/:id", element: <IncidentDetail /> },
        { path: "runbooks", element: <Runbooks /> },
        { path: "fixes", element: <Fixes /> },
        { path: "fleet", element: <Fleet /> },
        { path: "settings", element: <Settings /> },
      ],
    },
  ],
  { basename: "/console" },
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
