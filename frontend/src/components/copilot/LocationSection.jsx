import NetworkStatus from "../NetworkStatus";

// The network at the registered area shows as soon as the card loads. When the customer reports a network
// problem, the agent confirms the area by voice and the answer is picked up from the call.
export default function LocationSection({ call }) {
  const registered = call.card.registered_status;
  const current = call.currentLocation;
  const differs = current && registered && current.area !== registered.area;

  return (
    <>
      <p className="section-label">Network at the registered location</p>
      <NetworkStatus status={registered} />
      {call.locationPrompt && !current && !call.ended && (
        <p className="prompt-banner">
          The customer reported a network problem. Confirm where they are right now, for example &ldquo;Are you in{" "}
          {registered?.area} now?&rdquo;. Their answer is picked up from the call.
        </p>
      )}
      {current && (
        <>
          <p className="section-label">Where the customer is now (confirmed on the call)</p>
          <NetworkStatus status={current} />
          {differs && <p className="muted small">Different from the registered location ({registered.area}).</p>}
        </>
      )}
    </>
  );
}
