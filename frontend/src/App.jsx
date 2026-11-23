import { useState } from "react";
import Header from "./components/Header";
import DemoControls from "./components/DemoControls";
import LiveCallPage from "./pages/LiveCallPage";
import DashboardPage from "./pages/DashboardPage";
import { usePanelStream } from "./hooks/usePanelStream";
import { useSoftphone } from "./hooks/useSoftphone";

export default function App() {
  const [view, setView] = useState("live");
  const { call, connected } = usePanelStream();
  const softphone = useSoftphone();

  return (
    <div className="app">
      <Header view={view} onChange={setView} connected={connected} sttStatus={call.sttStatus} />
      <main className="page">
        {view === "live" ? (
          <LiveCallPage call={call} softphone={softphone} />
        ) : (
          <DashboardPage />
        )}
      </main>
      <DemoControls />
    </div>
  );
}
