import { useMemo, useState } from "react";
import { formatDateTime } from "../state/callLabels";

const MOOD_LABEL = { calm: "Calm", frustrated: "Frustrated", angry: "Angry" };

function title(s) {
  return s.customer_name || s.mobile_number || "Unknown caller";
}

function matches(s, needle) {
  if (!needle) return true;
  return [s.customer_name, s.mobile_number, s.ticket_id, s.subject, s.category, s.call_id]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(needle));
}

function Item({ session, selected, onSelect }) {
  return (
    <li>
      <button
        type="button"
        className={`session-item ${selected ? "selected" : ""}`}
        onClick={() => onSelect(session.call_id)}
        aria-current={selected ? "true" : undefined}
      >
        <span className="session-item-top">
          <strong>{title(session)}</strong>
          <span className="muted small">{formatDateTime(session.started_at)}</span>
        </span>
        <span className="session-item-subject">{session.subject || "No summary written"}</span>
        <span className="session-item-tags">
          {session.category && <span className="pill">{session.category}</span>}
          {session.ticket_id && <span className="pill pill-ok">{session.ticket_id}</span>}
          {session.mood && session.mood !== "calm" && (
            <span className={`pill ${session.mood === "angry" ? "pill-danger" : "pill-warn"}`}>
              {MOOD_LABEL[session.mood]}
            </span>
          )}
        </span>
      </button>
    </li>
  );
}

// Left sidebar: the calls saved so far. Opens wide to list them, closes to a narrow strip.
export default function SessionSidebar({ open, onToggle, sessions, loading, error, selectedId, onSelect, onReload }) {
  const [search, setSearch] = useState("");
  const needle = search.trim().toLowerCase();
  const shown = useMemo(() => sessions.filter((s) => matches(s, needle)), [sessions, needle]);

  return (
    <aside className={`session-sidebar ${open ? "open" : "closed"}`} aria-label="Session history">
      <div className="session-sidebar-head">
        {open && <h2>Session history</h2>}
        <button
          type="button"
          className="btn btn-ghost btn-small"
          onClick={onToggle}
          aria-expanded={open}
          title={open ? "Close the session history" : "Open the session history"}
        >
          {open ? "\u25C0 Close" : `\u25B6${sessions.length ? ` ${sessions.length}` : ""}`}
        </button>
      </div>
      {open && (
        <>
          <div className="session-tools">
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Name, number, ticket or subject"
              aria-label="Search sessions"
            />
            <button type="button" className="btn btn-ghost btn-small" onClick={onReload} title="Reload the list">
              Reload
            </button>
          </div>
          <button
            type="button"
            className={`session-item session-live ${selectedId ? "" : "selected"}`}
            onClick={() => onSelect(null)}
          >
            <strong>Live call</strong>
            <span className="muted small">The call on screen now</span>
          </button>
          {error && <p className="error small">{error}</p>}
          {loading && <p className="empty">Loading sessions&hellip;</p>}
          {!loading && !error && sessions.length === 0 && (
            <p className="empty">No saved sessions yet. A call is saved once it is summarised.</p>
          )}
          {!loading && sessions.length > 0 && shown.length === 0 && <p className="empty">Nothing matches.</p>}
          <ul className="session-list">
            {shown.map((s) => (
              <Item key={s.call_id} session={s} selected={s.call_id === selectedId} onSelect={onSelect} />
            ))}
          </ul>
        </>
      )}
    </aside>
  );
}
