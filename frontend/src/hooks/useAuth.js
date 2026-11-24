import { useCallback, useEffect, useState } from "react";
import { api, hasToken, setToken, setUnauthorizedHandler } from "../api/client";

const SIGNED_OUT = { status: "signed-out", user: null, demoControls: false, message: "" };

// Sign-in state. status: "checking" (validating a stored token), "signed-out" or "signed-in".
export function useAuth() {
  const [state, setState] = useState(() =>
    hasToken() ? { ...SIGNED_OUT, status: "checking" } : SIGNED_OUT
  );

  const signOut = useCallback((message = "") => {
    setToken("");
    setState({ ...SIGNED_OUT, message });
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => signOut("Your session has ended. Please sign in again."));
    return () => setUnauthorizedHandler(null);
  }, [signOut]);

  useEffect(() => {
    if (state.status !== "checking") return;
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) {
          setState({ status: "signed-in", user: me.user, demoControls: me.demo_controls_enabled, message: "" });
        }
      })
      .catch((error) => {
        // A 401 has already signed us out through the handler above.
        if (!cancelled && error.status !== 401) signOut(error.message);
      });
    return () => {
      cancelled = true;
    };
  }, [state.status, signOut]);

  const signIn = useCallback(async (username, password) => {
    const body = await api.login(username, password);
    setToken(body.token);
    setState({ ...SIGNED_OUT, status: "checking" });
  }, []);

  return { ...state, signIn, signOut };
}
