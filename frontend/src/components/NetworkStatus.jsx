const LABEL = {
  maintenance: "Maintenance",
  scheduled: "Planned work",
  outage: "Outage",
  no_notice: "No notice on file",
};

// One area's network status. "No notice on file" never means the network is confirmed fine.
export default function NetworkStatus({ status }) {
  if (!status) return null;
  return (
    <div className={`net-status net-${status.status}`}>
      <div className="net-head">
        <strong>{status.area}</strong>
        <span className="net-badge">{LABEL[status.status] || status.status}</span>
      </div>
      <span>
        {status.message}
        {status.until ? `. Until ${status.until}.` : "."}
      </span>
    </div>
  );
}
