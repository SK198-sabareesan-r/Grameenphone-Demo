import { api } from "../api/client";

function PackLine({ pack }) {
  return (
    <p className="suggestion-detail">
      <strong>{pack.name}</strong>: Tk {pack.price_bdt}, {pack.validity_days} days
      {pack.activation_code ? `, dial ${pack.activation_code}` : ""}
      {pack.sample_offer ? " (sample)" : ""}
    </p>
  );
}

function Centres({ centres }) {
  if (!centres.length) return null;
  return (
    <ul className="suggestion-centres">
      {centres.map((c) => (
        <li key={`${c.area}-${c.name}`}>
          <strong>{c.name}</strong>
          {c.type === "sample" ? " (sample)" : ""}
          {c.hours ? `, open ${c.hours}` : ""}
          <span className="muted small"> · {c.address}</span>
        </li>
      ))}
    </ul>
  );
}

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
              {s.pack && <PackLine pack={s.pack} />}
              {s.centres && <Centres centres={s.centres} />}
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
