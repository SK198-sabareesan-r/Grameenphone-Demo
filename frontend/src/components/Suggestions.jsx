import { api } from "../api/client";

export default function Suggestions({ items }) {
  return (
    <div className="card">
      <h2>Next best action</h2>
      {items.length === 0 ? (
        <p className="empty">Suggestions appear at the right moment.</p>
      ) : (
        items.map((s) => (
          <div key={s.id} className={`suggestion ${s.used ? "used" : ""}`}>
            <div>
              <strong>{s.title}</strong>
              <p>{s.text}</p>
            </div>
            {s.used ? (
              <span className="pill pill-ok">Used</span>
            ) : (
              <button className="btn btn-ghost" onClick={() => api.useSuggestion(s.id).catch(() => {})}>
                Mark used
              </button>
            )}
          </div>
        ))
      )}
      <p className="muted small">The agent decides. Copilot only suggests.</p>
    </div>
  );
}
