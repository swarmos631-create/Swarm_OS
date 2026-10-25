import { useEffect, useState } from "react";

import { Icon } from "../../lib/icons";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let updateSW: ((reload?: boolean) => Promise<void>) | null = null;
const listeners = new Set<() => void>();
let needRefresh = false;

function announceUpdate() {
  if (needRefresh) return;
  needRefresh = true;
  listeners.forEach((l) => l());
}

/** Registers the service worker once (production builds only) and surfaces update/install affordances. */
export async function initServiceWorker() {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) return;
  // A page opened before a deployment may ask for code-split files that no longer exist: load the new version once.
  window.addEventListener("vite:preloadError", (e) => {
    try {
      if (sessionStorage.getItem("swarmos.reloaded")) return;
      sessionStorage.setItem("swarmos.reloaded", "1");
    } catch { /* storage blocked: still reload once */ }
    e.preventDefault();
    window.location.reload();
  });
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    // A new version took over. Never reload while someone is looking at the page: offer the button, and apply it
    // on its own only once the tab is in the background.
    if (!hadController) return;
    announceUpdate();
    if (document.visibilityState === "hidden") window.location.reload();
    else document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") window.location.reload(); }, { once: true });
  });
  const { registerSW } = await import("virtual:pwa-register");
  updateSW = registerSW({
    immediate: true,
    onNeedRefresh: announceUpdate,
    onRegisteredSW(_url, reg) {
      // Look for a new deployment every 15 minutes and whenever the tab becomes visible again.
      if (!reg) return;
      setInterval(() => void reg.update(), 15 * 60_000);
      document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") void reg.update(); });
    },
  });
}

export function PwaControls() {
  const [refresh, setRefresh] = useState(needRefresh);
  const [installEvt, setInstallEvt] = useState<BeforeInstallPromptEvent | null>(null);

  useEffect(() => {
    const l = () => setRefresh(true);
    listeners.add(l);
    const onPrompt = (e: Event) => { e.preventDefault(); setInstallEvt(e as BeforeInstallPromptEvent); };
    window.addEventListener("beforeinstallprompt", onPrompt);
    const onInstalled = () => setInstallEvt(null);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      listeners.delete(l);
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  return (
    <>
      {refresh && (
        <button className="btn sm primary" onClick={() => { if (updateSW) void updateSW(true); window.setTimeout(() => window.location.reload(), 600); }} title="A new version of SwarmOS is available">
          <Icon name="refresh" />New version — reload
        </button>
      )}
      {installEvt && (
        <button className="btn sm hide-sm" onClick={async () => { await installEvt.prompt(); await installEvt.userChoice; setInstallEvt(null); }}>
          <Icon name="download" />Install app
        </button>
      )}
    </>
  );
}
