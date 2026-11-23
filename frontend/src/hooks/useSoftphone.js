import { useCallback, useEffect, useRef, useState } from "react";
import { Device } from "@twilio/voice-sdk";
import { api } from "../api/client";

// Browser softphone for the agent. States: offline, connecting, ready, ringing, in-call, error.
export function useSoftphone() {
  const deviceRef = useRef(null);
  const callRef = useRef(null);
  const [status, setStatus] = useState("offline");
  const [message, setMessage] = useState("");

  const attach = useCallback((call) => {
    callRef.current = call;
    call.on("accept", () => setStatus("in-call"));
    const done = () => {
      callRef.current = null;
      setStatus("ready");
    };
    call.on("disconnect", done);
    call.on("cancel", done);
    call.on("reject", done);
  }, []);

  useEffect(() => {
    let device;
    let cancelled = false;

    async function start() {
      setStatus("connecting");
      try {
        const { token } = await api.softphoneToken();
        if (cancelled) return;
        device = new Device(token, { codecPreferences: ["opus", "pcmu"] });
        device.on("registered", () => setStatus("ready"));
        device.on("error", (error) => {
          setMessage(error.message);
          setStatus("error");
        });
        device.on("incoming", (call) => {
          attach(call);
          setStatus("ringing");
        });
        device.on("tokenWillExpire", async () => device.updateToken((await api.softphoneToken()).token));
        deviceRef.current = device;
        await device.register();
      } catch (error) {
        setMessage(error.message);
        setStatus("error");
      }
    }

    start();
    return () => {
      cancelled = true;
      device?.destroy();
    };
  }, [attach]);

  const answer = () => callRef.current?.accept();
  const hangUp = () => callRef.current?.disconnect();

  return { status, message, answer, hangUp };
}
