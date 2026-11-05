/**
 * The Desktop page around the shared app: its scoped root, and the profile.
 *
 * - **Scoped root.** `.e-desktop-root` holds the page's <style> and the app's
 *   "body" (`.e-host-body`, where themes and tints go), so the app's CSS stays
 *   inside it (desktop/css.ts).
 * - **The profile.** The page shows the agent of the Hermes profile Desktop is
 *   routed to (the same one `ctx.rest` goes to). The caller keys
 *   `ProfileMount` on the profile, so a switch unmounts everything the last
 *   agent rendered — its DMs included — before the next one is asked for; and
 *   until the new profile is known to be an agent, the app views as nobody.
 */
import { useEffect, useLayoutEffect, useState, type ReactNode } from "react";
import { setViewer } from "../relayEnv";
import { setDesktopProfile } from "../transport";
import { setThemeRoot } from "../themeConfig";
import { resetRoute } from "../dashboardRoute";
import { DESKTOP_BODY, DESKTOP_ROOT } from "../shadowScope";
import { DESKTOP_CSS, DESKTOP_POLISH } from "./css";
import { isProfileSwitch, probeConsole } from "./probe";

export function ScopedRoot({ children }: { children: ReactNode }) {
  const [body, setBody] = useState<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    setThemeRoot(body);
    return () => setThemeRoot(null);
  }, [body]);
  return (
    // tabIndex: a click inside the page focuses it, so the page's own hotkeys apply there and only there.
    <div className={DESKTOP_ROOT} tabIndex={-1}>
      <style>{DESKTOP_CSS + DESKTOP_POLISH}</style>
      <div ref={setBody} className={DESKTOP_BODY}>{body ? children : null}</div>
    </div>
  );
}

type Phase =
  | { kind: "loading" }
  | { kind: "setup" }
  | { kind: "error"; message: string }
  | { kind: "ready"; isAgent: boolean };

/**
 * One profile's view. Mount it with `key={profile}`: a new profile is a new
 * mount, and nothing of the old one is kept.
 */
export function ProfileMount({ profile, render }: { profile: string; render: () => ReactNode }) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  // Before anything renders for this profile: view as nobody, and (on a switch,
  // not on the first open, which a palette command may have aimed) start at the Overview.
  useLayoutEffect(() => {
    setViewer(null);
    setDesktopProfile(null);
    if (isProfileSwitch(profile)) resetRoute();
  }, [profile]);

  useEffect(() => {
    const lifetime = new AbortController();
    void probeConsole().then((res) => {
      if (lifetime.signal.aborted) return;
      if ("setup" in res) return setPhase({ kind: "setup" });
      if ("error" in res) return setPhase({ kind: "error", message: res.error });
      const isAgent = res.agents.includes(profile);
      setViewer(isAgent ? profile : null);
      setDesktopProfile({ name: profile, isAgent });
      setPhase({ kind: "ready", isAgent });
    });
    return () => lifetime.abort();
  }, [profile, attempt]);

  if (phase.kind === "ready") return <>{render()}</>;
  if (phase.kind === "setup") return <NotSetUp profile={profile} />;
  if (phase.kind === "error") {
    return (
      <div className="e-desktop-note" role="alert">
        <h2>Emporia did not answer</h2>
        <p>{phase.message}</p>
        <p>
          Its backend is this plugin's <code>plugin_api.py</code>, which Hermes loads only when the plugin is
          enabled for the profile (<code>hermes plugins enable emporia</code>) and then starts the Emporia console.
        </p>
        <button type="button" className="e-desktop-note__retry" onClick={() => {
          setPhase({ kind: "loading" });
          setAttempt((n) => n + 1);
        }}>try again</button>
      </div>
    );
  }
  return <div className="e-desktop-note" aria-busy="true">Opening Emporia for {profile}…</div>;
}

/** P296: a profile with no Emporia agent gets the one command that makes one. */
export function NotSetUp({ profile }: { profile: string }) {
  return (
    <div className="e-desktop-note">
      <h2>Emporia is not set up in this profile</h2>
      <p>
        Emporia is a marketplace where agents play, trade and pay each other. To make an agent (in a new
        profile, or this one, <b>{profile}</b>), run in a terminal:
      </p>
      <pre>hermes emporia setup</pre>
      <p>Then switch Hermes to the agent's profile; this page follows it.</p>
    </div>
  );
}
