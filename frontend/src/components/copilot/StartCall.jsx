import { useState } from "react";
import { api } from "../../api/client";

// Start a call on the panel without telephony. With a caller ID the customer card appears at once, marked
// "not confirmed", as it does for a phone call that reaches the human agent.
export default function StartCall() {
  const [caller, setCaller] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.startCall(caller.trim());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="start-call" onSubmit={submit}>
      <input
        value={caller}
        onChange={(e) => setCaller(e.target.value)}
        placeholder="Caller ID, for example 9500799295"
        inputMode="tel"
        autoComplete="off"
        aria-label="Caller ID"
      />
      <button className="btn btn-primary" type="submit" disabled={busy}>
        Start call
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}
