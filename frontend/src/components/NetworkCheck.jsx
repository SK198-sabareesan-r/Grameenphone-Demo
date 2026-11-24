import { useEffect, useState } from "react";
import { api } from "../api/client";
import NetworkStatus from "./NetworkStatus";

// The agent asks where the customer is right now and checks the network status at that location.
export default function NetworkCheck({ prompt, current, registered, callActive, callEnded }) {
  const [areas, setAreas] = useState([]);
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.locations().then((r) => setAreas(r.locations)).catch(() => {});
  }, []);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.setLocation(value.trim());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const differs = current && registered && current.area !== registered.area;
  return (
    <div className="card">
      <h2>Network check</h2>
      {prompt && !current && (
        <p className="prompt-banner">
          The customer reported a network problem. Ask “Where are you right now?” and enter the location below.
        </p>
      )}
      <form className="location-form" onSubmit={submit}>
        <input
          list="gp-areas"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Customer's current location"
          autoComplete="off"
        />
        <datalist id="gp-areas">
          {areas.map((a) => (
            <option key={a} value={a} />
          ))}
        </datalist>
        <button className="btn btn-primary" type="submit" disabled={!callActive || callEnded || busy || !value.trim()}>
          Check
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {current ? (
        <>
          <p className="section-label">Customer's current location</p>
          <NetworkStatus status={current} />
          {differs && (
            <p className="muted small">Different from the registered location ({registered.area}).</p>
          )}
        </>
      ) : (
        <p className="empty">No location checked yet.</p>
      )}
    </div>
  );
}
