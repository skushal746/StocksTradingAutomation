import { useEffect, useState } from "react";
import { api, ApiError, getStoredToken, storeToken } from "./api";
import { AuthView } from "./components/AuthView";
import { Dashboard } from "./components/Dashboard";
import type { User } from "./types";

export default function App() {
  const [token, setToken] = useState<string | null>(() => getStoredToken());
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(Boolean(token));

  // When a token is stored (for example after a page reload), check it is still valid.
  useEffect(() => {
    if (!token) {
      setChecking(false);
      return;
    }
    let cancelled = false;
    api
      .me(token)
      .then((u) => !cancelled && setUser(u))
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) endSession();
        else setUser(null);
      })
      .finally(() => !cancelled && setChecking(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  function endSession() {
    storeToken(null);
    setToken(null);
    setUser(null);
  }

  function logIn(newToken: string) {
    setChecking(true);
    setToken(newToken);
  }

  if (checking) return <p className="muted center">Loading...</p>;
  if (!token || !user) return <AuthView onLoggedIn={logIn} />;
  return <Dashboard token={token} user={user} onLogout={endSession} onSessionExpired={endSession} />;
}
