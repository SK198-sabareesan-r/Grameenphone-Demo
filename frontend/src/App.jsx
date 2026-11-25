import { useState } from "react";
import Header from "./components/Header";
import LiveCallPage from "./pages/LiveCallPage";
import DashboardPage from "./pages/DashboardPage";
import TicketsPage from "./pages/TicketsPage";
import LoginPage from "./pages/LoginPage";
import { useAuth } from "./hooks/useAuth";
import { usePanelStream } from "./hooks/usePanelStream";

// The console only mounts once signed in, so the panel WebSocket opens with a valid session and
// closes on sign-out.
function Console({ auth }) {
  const [view, setView] = useState("live");
  const { call, connected } = usePanelStream();

  return (
    <div className="app">
      <Header
        view={view}
        onChange={setView}
        connected={connected}
        user={auth.user}
        onSignOut={auth.signOut}
      />
      <main className="page">
        {view === "live" && <LiveCallPage call={call} />}
        {view === "tickets" && <TicketsPage />}
        {view === "dashboard" && <DashboardPage />}
      </main>
    </div>
  );
}

export default function App() {
  const auth = useAuth();
  if (auth.status === "checking") return <p className="muted page">Checking sign-in…</p>;
  if (auth.status !== "signed-in") return <LoginPage onSignIn={auth.signIn} message={auth.message} />;
  return <Console auth={auth} />;
}
