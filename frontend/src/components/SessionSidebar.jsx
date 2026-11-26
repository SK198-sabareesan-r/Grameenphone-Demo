import { useMemo, useState } from "react";

const MOOD_LABEL = { frustrated: "Frustrated customer", angry: "Angry customer" };

const DAY = 86400;

function Icon({ name }) {
  const paths = {
    panel: (
      <>
        <rect x="3" y="4" width="18" height="16" rx="3" />
        <path d="M9 4v16" />
      </>
    ),
    phone: (
      <path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z" />
    ),
    search: (
      <>
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.5-3.5" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 11a8 8 0 1 0-2.3 5.7" />
        <path d="M20 4v7h-7" />
      </>
    ),
  };
  return (
    <svg
      className="si-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}

function title(s) {
  return s.customer_name || s.mobile_number || "Unknown caller";
}

function matches(s, needle) {
  if (!needle) return true;
  return [s.customer_name, s.mobile_number, s.ticket_id, s.subject, s.category, s.call_id]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(needle));
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime() / 1000;
}

// Today, Yesterday, Previous 7 days, Previous 30 days, then one group per month.
function groupLabel(ts, today) {
  if (!ts) return "Earlier";
  const day = startOfDay(new Date(ts * 1000));
  if (day >= today) return "Today";
  if (day >= today - DAY) return "Yesterday";
  if (day >= today - 7 * DAY) return "Previous 7 days";
  if (day >= today - 30 * DAY) return "Previous 30 days";
  return new Date(ts * 1000).toLocaleDateString([], { month: "long", year: "numeric" });
}

function groupSessions(sessions) {
  const today = startOfDay(new Date());
  const groups = [];
  for (const s of sessions) {
    const label = groupLabel(s.started_at, today);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(s);
    else groups.push({ label, items: [s] });
  }
  return groups;
}

function clock(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function Row({ session, selected, onSelect }) {
  const moodLabel = MOOD_LABEL[session.mood];
  const tip = [session.subject, session.ticket_id, session.category].filter(Boolean).join(" \u00B7 ");
  return (
    <li>
      <button
        type="button"
        className={`si-row ${selected ? "selected" : ""}`}
        onClick={() => onSelect(session.call_id)}
        aria-current={selected ? "true" : undefined}
        title={tip || undefined}
      >
        <span className="si-row-main">
          <span className="si-row-title">
            {moodLabel && <span className={`si-dot si-dot-${session.mood}`} role="img" aria-label={moodLabel} title={moodLabel} />}
            {title(session)}
          </span>
          <span className="si-row-sub">{session.subject || "No summary"}</span>
        </span>
        <span className="si-row-time">{clock(session.started_at)}</span>
      </button>
    </li>
  );
}

// Left sidebar listing the saved calls, in the style of a chat history: newest first, grouped by day,
// one quiet row per call. It opens wide and closes to a slim rail with the same two actions.
export default function SessionSidebar({ open, onToggle, sessions, loading, error, selectedId, onSelect, onReload }) {
  const [search, setSearch] = useState("");
  const needle = search.trim().toLowerCase();
  const groups = useMemo(() => groupSessions(sessions.filter((s) => matches(s, needle))), [sessions, needle]);
  const liveSelected = !selectedId;

  if (!open) {
    return (
      <aside className="session-sidebar closed" aria-label="Session history">
        <button type="button" className="si-icon-btn" onClick={onToggle} aria-expanded="false" title="Open session history">
          <Icon name="panel" />
        </button>
        <button
          type="button"
          className={`si-icon-btn ${liveSelected ? "selected" : ""}`}
          onClick={() => onSelect(null)}
          title="Live call"
        >
          <Icon name="phone" />
        </button>
      </aside>
    );
  }

  return (
    <aside className="session-sidebar open" aria-label="Session history">
      <div className="si-top">
        <span className="si-heading">Sessions</span>
        <div className="si-top-actions">
          <button type="button" className="si-icon-btn" onClick={onReload} title="Reload the list">
            <Icon name="refresh" />
          </button>
          <button type="button" className="si-icon-btn" onClick={onToggle} aria-expanded="true" title="Close session history">
            <Icon name="panel" />
          </button>
        </div>
      </div>

      <button type="button" className={`si-live ${liveSelected ? "selected" : ""}`} onClick={() => onSelect(null)}>
        <Icon name="phone" />
        <span>Live call</span>
      </button>

      <label className="si-search">
        <Icon name="search" />
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search sessions"
          aria-label="Search sessions"
        />
      </label>

      <div className="si-scroll">
        {error && <p className="error small si-note">{error}</p>}
        {loading && <p className="si-note muted small">Loading&hellip;</p>}
        {!loading && !error && sessions.length === 0 && (
          <p className="si-note muted small">No saved sessions yet. A call is saved once it is summarised.</p>
        )}
        {!loading && sessions.length > 0 && groups.length === 0 && <p className="si-note muted small">No matches.</p>}
        {groups.map((g) => (
          <section key={g.label} className="si-group">
            <h3>{g.label}</h3>
            <ul>
              {g.items.map((s) => (
                <Row key={s.call_id} session={s} selected={s.call_id === selectedId} onSelect={onSelect} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </aside>
  );
}
