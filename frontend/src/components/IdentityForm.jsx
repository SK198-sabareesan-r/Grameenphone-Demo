import { useEffect, useState } from "react";
import { api } from "../api/client";

// Shown instead of the customer card. Nothing about the customer loads until the agent confirms the number.
export default function IdentityForm({ callerNumber, callActive, callEnded }) {
  const [number, setNumber] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // The caller ID only pre-fills the box. The agent must still confirm the number with the customer.
  useEffect(() => {
    if (callerNumber) setNumber(callerNumber);
  }, [callerNumber]);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.confirmIdentity(number.trim());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const disabled = !callActive || callEnded || busy || !number.trim();
  return (
    <div className="card">
      <h2>Customer</h2>
      <p className="empty">No account data is shown until you confirm the customer's mobile number.</p>
      <form className="identity-form" onSubmit={submit}>
        <label>
          Mobile number confirmed by the customer
          <input
            value={number}
            onChange={(e) => setNumber(e.target.value)}
            placeholder="01XXXXXXXXX"
            inputMode="tel"
            autoComplete="off"
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={disabled}>
          Confirm and show card
        </button>
      </form>
      {callerNumber && <p className="muted small">Pre-filled from the caller ID. Ask the customer to confirm it.</p>}
      {!callActive && <p className="muted small">Available once a call is connected.</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
