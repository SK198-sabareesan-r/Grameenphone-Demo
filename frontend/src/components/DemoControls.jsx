import { useEffect, useState } from "react";
import { api } from "../api/client";

export default function DemoControls() {
  const [customers, setCustomers] = useState([]);
  const [active, setActive] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.customers(), api.activeCustomer()])
      .then(([list, current]) => {
        setCustomers(list.customers);
        setActive(current.active);
      })
      .catch((e) => setError(e.message));
  }, []);

  async function pick(event) {
    const id = event.target.value;
    setActive(id);
    await api.setActiveCustomer(id).catch((e) => setError(e.message));
  }

  async function reset() {
    if (window.confirm("Clear the current call, tickets and saved calls?")) {
      await api.resetData().catch((e) => setError(e.message));
    }
  }

  return (
    <details className="demo-controls">
      <summary>Demo controls</summary>
      <label>
        Customer for the next call
        <select value={active} onChange={pick}>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <button className="btn btn-ghost" onClick={reset}>
        Reset demo data
      </button>
      {error && <p className="error">{error}</p>}
    </details>
  );
}
