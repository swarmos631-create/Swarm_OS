import { Panel } from "../components/ui";
import { Icon, type IconName } from "../lib/icons";
import { ACCENTS, useTheme, type ThemePref } from "../state/theme";

const MODES: { id: ThemePref; label: string; hint: string; icon: IconName }[] = [
  { id: "light", label: "Light", hint: "Bright background", icon: "sun" },
  { id: "dark", label: "Dark", hint: "Easy on the eyes at night", icon: "moon" },
  { id: "system", label: "System", hint: "Follow this device", icon: "monitor" },
];

/** Appearance settings: light / dark / system mode and the colour theme that replaces the brand gradient everywhere. */
export default function SettingsPage() {
  const { pref, setPref, accent, setAccent, theme } = useTheme();
  const current = ACCENTS.find((a) => a.id === accent) ?? ACCENTS[0];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Appearance is saved on this device and applies to every page and the MapmyIndia maps.</p>
        </div>
        <button className="btn ghost" onClick={() => { setPref("system"); setAccent("aurora"); }}><Icon name="refresh" />Reset to default</button>
      </div>

      <Panel title="Mode" icon="sun">
        <div className="mode-cards" role="group" aria-label="Light or dark mode">
          {MODES.map((m) => (
            <button key={m.id} type="button" className="mode-card" aria-pressed={pref === m.id} onClick={() => setPref(m.id)} data-testid={`mode-${m.id}`}>
              <Icon name={m.icon} />
              <span className="stack" style={{ gap: 0 }}><span>{m.label}</span><span className="xs faint" style={{ fontWeight: 500 }}>{m.hint}</span></span>
            </button>
          ))}
        </div>
        <p className="xs faint" style={{ marginTop: 10 }}>Currently showing the {theme} version.</p>
      </Panel>

      <Panel title="Colour theme" icon="palette">
        <div className="swatches" role="group" aria-label="Colour theme">
          {ACCENTS.map((a) => (
            <button key={a.id} type="button" className="swatch" aria-pressed={accent === a.id} onClick={() => setAccent(a.id)} data-testid={`accent-${a.id}`}>
              <span className="preview" style={{ background: `linear-gradient(135deg, ${a.from}, ${a.to})` }} />
              <span className="row" style={{ gap: 6 }}>
                <span className="stack" style={{ gap: 0 }}><b>{a.name}</b><span className="xs faint">{a.hint}</span></span>
                {accent === a.id && <span className="check"><Icon name="check" width={12} height={12} /></span>}
              </span>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title={`Preview — ${current.name}`} icon="grid">
        <div className="row wrap" style={{ gap: 12 }}>
          <button className="btn primary"><Icon name="workspace" />Primary action</button>
          <button className="btn"><Icon name="map" />Secondary</button>
          <div className="segmented" role="group" aria-label="Preview filter">
            <button type="button" aria-pressed="true">Active <span className="count">6</span></button>
            <button type="button" aria-pressed="false">Idle</button>
          </div>
          <span className="badge b-live"><span className="dot" />Live</span>
          <span className="badge b-demo"><span className="dot" />Simulated</span>
          <span className="avatar">SO</span>
        </div>
        <div className="progress" style={{ marginTop: 14 }}><span style={{ width: "68%" }} /></div>
      </Panel>

    </>
  );
}
