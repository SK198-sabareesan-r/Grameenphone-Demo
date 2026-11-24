import { useState } from "react";
import { api } from "../api/client";

export default function DemoControls() {
  const [error, setError] = useState("");

  async function reset() {
    if (window.confirm("Clear the current call, tickets and saved calls?")) {
      await api.resetData().catch((e) => setError(e.message));
    }
  }

  return (
    <details className="demo-controls">
      <summary>Demo controls</summary>
      <button className="btn btn-ghost" onClick={reset}>
        Reset demo data
      </button>
      {error && <p className="error">{error}</p>}
    </details>
  );
}
