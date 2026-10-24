import type { SVGProps } from "react";

/** Minimal stroke icon set drawn for SwarmOS (24×24 grid, currentColor). */
const P: Record<string, string> = {
  command: "M3 12h4l3-8 4 16 3-8h4",
  map: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14",
  agents: "M12 3l7 4v10l-7 4-7-4V7zM12 8v8M8.5 10l7 4M15.5 10l-7 4",
  workspace: "M4 5h16v11H8l-4 4zM8 9h8M8 12h5",
  incidents: "M12 3 2 20h20zM12 9v5M12 17.5v.5",
  analytics: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  integrations: "M8 7h8M8 17h8M5 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 14a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM5 10v4a3 3 0 0 0 3 3M19 14v-4a3 3 0 0 0-3-3",
  models: "M4 7l8-4 8 4-8 4zM4 12l8 4 8-4M4 17l8 4 8-4",
  system: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.5-4.5",
  menu: "M4 6h16M4 12h16M4 18h16",
  close: "M6 6l12 12M18 6 6 18",
  chevron: "M9 6l6 6-6 6",
  chevronDown: "M6 9l6 6 6-6",
  pin: "M12 21s-7-6.3-7-12a7 7 0 1 1 14 0c0 5.7-7 12-7 12zM12 6.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  play: "M7 4v16l13-8z",
  stop: "M6 6h12v12H6z",
  check: "M4 12l5 5L20 6",
  x: "M6 6l12 12M18 6 6 18",
  alert: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v6M12 16.5v.5",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v6M12 7.5V8",
  wifiOff: "M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 12.5a10 10 0 0 1 5.2-2.7M19 12.5a10 10 0 0 0-3.4-2.2M2 8.8a15 15 0 0 1 4.2-2.6M22 8.8a15 15 0 0 0-11.5-3.7M12 20h.01",
  download: "M12 3v12M6 11l6 6 6-6M4 21h16",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11",
  plus: "M12 5v14M5 12h14",
  layers: "M12 3 2 8l10 5 10-5zM2 13l10 5 10-5",
  cube: "M12 2l9 5v10l-9 5-9-5V7zM12 12l9-5M12 12v10M12 12 3 7",
  bell: "M6 16V11a6 6 0 1 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  doc: "M6 3h9l5 5v13H6zM14 3v6h6M9 13h7M9 17h7",
  database: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
};

export type IconName = keyof typeof P;

export function Icon({ name, ...rest }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...rest}>
      <path d={P[name]} />
    </svg>
  );
}
