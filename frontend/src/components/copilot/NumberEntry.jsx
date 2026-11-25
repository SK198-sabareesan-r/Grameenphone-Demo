import { useEffect, useState } from "react";
import { api } from "../../api/client";

// The second way to confirm the customer's number. The first is automatic: the number is picked up from the
// call. Here the agent types the number the customer gave, or presses the button for the number already
// shown, once the customer has said it is right. A different number loads that customer instead.
export default function NumberEntry({ initial, label, placeholder, hint, callActive, callEnded }) {
  const [number, setNumber] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (initial) setNumber(initial);
  }, [initial]);

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

  return (
    <form className="number-entry" onSubmit={submit}>
      <input
        value={number}
        onChange={(e) => setNumber(e.target.value)}
        placeholder={placeholder}
        inputMode="tel"
        autoComplete="off"
        aria-label="Customer mobile number"
      />
      <button className="btn btn-primary" type="submit" disabled={!callActive || callEnded || busy || !number.trim()}>
        {label}
      </button>
      {error && <p className="error">{error}</p>}
      {hint && <p className="muted small">{hint}</p>}
    </form>
  );
}
