import { useRelayCtx } from "../relayCtx";
import AlertBanner from "@components/AlertBanner";
import Badge from "@components/Badge";
import RowSpaceBetween from "@components/RowSpaceBetween";
import Text from "@components/Text";
import { PaymentsFeesSection } from "../PaymentsFeesSection";
import { TrustSafetySection } from "../TrustSafetySection";
import { FederationSection } from "../FederationSection";
import {
  countCaptionForNav,
  countsFromSnapshot,
  failedFromSnapshot,
  fetchOverviewSnapshot,
  metricForNav,
  sectionsFromSnapshot,
  OVERVIEW_SKIP_VIEWS,
  type OverviewSectionRow,
  type OverviewTabId,
} from "../dashboardCounts";
import { eventDetail, viewForEvent } from "../eventNav";
import type { GlobalEvent, RelayHealth } from "../hooks";
import { useInterval } from "../hooks";
import { useRelayHealth } from "../healthCtx";
import { NAV_GROUPS, navById, type View } from "../navConfig";
import type { Navigate } from "../navigation";
import { RELAY, VIEWER_ID } from "../relayEnv";
import { FeedEventType, ViewBody } from "../ui/layout";
import { SlimCard } from "../ui/cards";

export function OverviewView({
  events,
  seed,
  wsConnected,
  navigate,
  onAfterNavigate,
}: {
  events: GlobalEvent[];
  seed: RelayHealth | null;
  wsConnected: boolean;
  navigate: Navigate;
  onAfterNavigate?: () => void;
}) {
  /* P43-2: one fanout, not two. `fetchDashboardCounts` and
     `fetchOverviewSections` each fetched the same seven collections on their
     own timer; the snapshot is fetched once and both are derived from it. */
  const { isRelayOperator } = useRelayCtx();
  const [snapshot] = useInterval(() => fetchOverviewSnapshot(VIEWER_ID, isRelayOperator), 15_000, [isRelayOperator]);
  const counts = snapshot ? countsFromSnapshot(snapshot) : null;
  /* P69: which collections *failed*, as opposed to came back empty. The
     snapshot is allSettled, so it always resolves and useInterval's `error`
     never fires here — the failure is per collection, and a rejected one used
     to render as "—", the same glyph as "not loaded yet", beside cards showing
     real numbers. */
  const failed = snapshot ? failedFromSnapshot(snapshot) : new Set<OverviewTabId>();
  const sections = snapshot ? sectionsFromSnapshot(snapshot) : null;
  const settlementRows =
    snapshot?.settlements.status === "fulfilled" ? snapshot.settlements.value.settlements : [];
  const health = useRelayHealth() ?? seed;
  const online = health?.status === "ok";
  const countsMerged = counts ?? null;

  const sectionByView = new Map<OverviewTabId, OverviewSectionRow>();
  for (const s of sections ?? []) sectionByView.set(s.view, s);

  const go = (v: View) => {
    navigate(v);
    onAfterNavigate?.();
  };

  const openEvent = (ev: GlobalEvent) => {
    const target = viewForEvent(ev);
    if (target) {
      navigate(target.view, target.opts);
      onAfterNavigate?.();
    }
  };

  const hubGroups = NAV_GROUPS.filter((g) => g.title !== "HOME")
    .map((g) => ({
      title: g.title,
      ids: g.ids.filter((id) => !OVERVIEW_SKIP_VIEWS.includes(id)),
    }))
    .filter((g) => g.ids.length > 0);

  const overviewIds = hubGroups.flatMap((g) => g.ids);

  const renderOverviewCard = (id: View) => {
    const item = navById(id);
    if (!item) return null;
    const tabId = id as OverviewTabId;
    const row = sectionByView.get(tabId);
    const didFail = failed.has(tabId);
    const metric = didFail
      ? "!"
      : row?.metric ?? metricForNav(item, health, countsMerged, events.length, online);
    const caption = didFail
      ? "unavailable"
      : countCaptionForNav(item, health, countsMerged, online);
    const lines = (row?.lines ?? [item.blurb]).slice(0, 3);
    return (
      <button
        key={id}
        type="button"
        className={didFail ? "e-overview-card e-overview-card--failed" : "e-overview-card"}
        onClick={() => go(id)}
        title={
          didFail
            ? `${item.label}: this collection did not load. The relay answered for the ` +
              `other panels, so this is one endpoint failing rather than the relay being ` +
              `down — open the tab for the error.`
            : [item.blurb, ...(row?.lines ?? [])].join("\n")
        }
      >
        <div className="e-overview-card__head">
          <div className="e-overview-card__topline">
            <span className="e-overview-card__icon" aria-hidden>
              {item.icon}
            </span>
            <span className="e-overview-card__title">{item.label}</span>
          </div>
          <span className="e-overview-card__sub">
            {caption || metric}
          </span>
        </div>
        <div className="e-overview-card__body">
          {lines.map((line) => (
            <p key={line}>{line}</p>
          ))}
        </div>
      </button>
    );
  };

  return (
    <ViewBody>
      {health && !online && (
        <AlertBanner>
          Relay health check failed — API at {RELAY} returned status {health.status}.
        </AlertBanner>
      )}

      <Text className="e-dim e-overview-status">
        {online ? "Relay connected" : health ? "Relay unreachable" : "Checking relay…"}
        {" · "}
        {wsConnected ? `${events.length} live feed events` : "Event stream reconnecting…"}
        {health?.guardrails_mode ? ` · guardrails ${health.guardrails_mode}` : ""}
        {health?.require_nous ? " · nous-gated" : ""}
      </Text>

      <Text className="e-faint e-pipeline-strip">
        Every inbound action: Guardrails → Ed25519 signature → Stripe gate → Proof-of-Reasoning → Audit log → Module dispatch
      </Text>

      <div className="e-overview-hub" role="list">
        {overviewIds.map((id) => renderOverviewCard(id))}
      </div>

      <SlimCard title="Live activity" action={<Badge>{events.length}</Badge>}>
        <div className="e-feed-panel e-feed-panel--v2">
          {events.length === 0 ? (
            <Text className="e-faint">Waiting for agent activity…</Text>
          ) : (
            events.slice(0, 50).map((ev) => {
              const ts = new Date(ev._ts).toLocaleTimeString();
              const jump = viewForEvent(ev);
              return (
                <div
                  key={`${ev._ts}-${ev.type}-${eventDetail(ev)}`}
                  className={`e-feed-row e-feed-row--v2 e-feed-row--slim${jump ? " is-clickable" : ""}`}
                  role={jump ? "button" : undefined}
                  tabIndex={jump ? 0 : undefined}
                  onClick={() => jump && openEvent(ev)}
                  onKeyDown={(e) => {
                    if (jump && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      openEvent(ev);
                    }
                  }}
                >
                  <FeedEventType type={ev.type} />
                  <RowSpaceBetween>
                    <Text className="e-feed-detail">{eventDetail(ev)}</Text>
                    <Text className="e-dim e-feed-time">{ts}</Text>
                  </RowSpaceBetween>
                </div>
              );
            })
          )}
        </div>
      </SlimCard>

      <PaymentsFeesSection settlements={settlementRows} navigate={navigate} />
      <TrustSafetySection />
      <FederationSection />
    </ViewBody>
  );
}