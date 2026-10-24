import { useEffect, useState } from "react";

import { Icon } from "../../lib/icons";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let updateSW: ((reload?: boolean) => Promise<void>) | null = null;
const listeners = new Set<() => void>();
let needRefresh = false;

/** Registers the service worker once (production builds only) and surfaces update/install affordances. */
export async function initServiceWorker() {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) return;
  const { registerSW } = await import("virtual:pwa-register");
  updateSW = registerSW({
    immediate: true,
    onNeedRefresh() {
      // Never reload while someone is looking at the page: show the "Update available" button in the shell, and
      // apply the new version on its own only once the tab is in the background (nothing is being read or typed).
      if (needRefresh) return;
      needRefresh = true;
      listeners.forEach((l) => l());
      const applyWhenHidden = () => {
        if (document.visibilityState !== "hidden") return;
        document.removeEventListener("visibilitychange", applyWhenHidden);
        void updateSW?.(true);
      };
      document.addEventListener("visibilitychange", applyWhenHidden);
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
        <button className="btn sm primary" onClick={() => void updateSW?.(true)} title="A new version of SwarmOS is available">
          <Icon name="refresh" />Update available — reload
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
