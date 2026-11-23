import { useEffect, useState } from "react";
import { api } from "../api/client";

function Kpi({ label, value, suffix = "" }) {
  return (
    <div className="card kpi">
      <span className="kpi-value">
        {value}
        {suffix}
      </span>
      <span className="muted">{label}</span>
    </div>
  );
}

function MoodTrend({ rows }) {
  return (
    <div className="card">
      <h2>Customer mood trend (% of calls)</h2>
      {rows.map((row) => (
        <div className="trend-row" key={row.day}>
          <span className="trend-day">{row.day}</span>
          <div className="trend-bar">
            <span className="seg mood-calm" style={{ width: `${row.calm}%` }} title={`Calm ${row.calm}`} />
            <span className="seg mood-frustrated" style={{ width: `${row.frustrated}%` }} title={`Frustrated ${row.frustrated}`} />
            <span className="seg mood-angry" style={{ width: `${row.angry}%` }} title={`Angry ${row.angry}`} />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function DashboardPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.dashboard().then(setData).catch((e) => setError(e.message));
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;

  const { kpis } = data;
  const acceptRate = Math.round((kpis.suggestions_accepted / Math.max(kpis.suggestions_shown, 1)) * 100);
  return (
    <div className="dashboard">
      <div className="kpi-grid">
        <Kpi label="Calls that used Copilot" value={kpis.calls_with_copilot} />
        <Kpi label="Suggestions shown" value={kpis.suggestions_shown} />
        <Kpi label="Suggestions accepted" value={acceptRate} suffix="%" />
        <Kpi label="Handling time saved per call" value={kpis.avg_handle_time_saved_sec} suffix=" s" />
      </div>
      <div className="dash-grid">
        <MoodTrend rows={data.mood_trend} />
        <div className="card">
          <h2>Most used suggestions</h2>
          {data.top_suggestions.map((s) => (
            <div className="top-row" key={s.title}>
              <span>{s.title}</span>
              <strong>{s.count}</strong>
            </div>
          ))}
        </div>
      </div>
      <p className="mock-note">
        {data.label}. Live calls on this machine so far: {data.live.calls}.
      </p>
    </div>
  );
}
