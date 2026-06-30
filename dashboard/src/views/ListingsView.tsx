import Text from "@components/Text";
import Indent from "@components/Indent";
import { api } from "../api";
import type { Listing } from "../api";
import { EMPTY_SEED_HINT } from "../navigation";
import type { Navigate } from "../navigation";
import { viewForListing } from "../listingNav";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { SlimCard } from "../ui/cards";
import { useInterval } from "../hooks";
import { AgentMoireAvatar } from "../AgentMoireAvatar";

type RowCells = (string | JSX.Element)[];

function ListingTable({
  headers,
  rows,
  listings,
  onOpen,
}: {
  headers: string[];
  rows: RowCells[];
  listings: Listing[];
  onOpen: (l: Listing) => void;
}) {
  return (
    <div className="e-compact-table e-listings-click" style={{ overflowX: "auto" }}>
      <table className="e-listings-native">
        <thead>
          <tr>
            {headers.map((h) => (
              <td key={h}>{h}</td>
            ))}
          </tr>
        </thead>
        <tbody>
          {listings.map((l, i) => (
            <tr
              key={l.listing_id}
              className="e-listings-row"
              role="button"
              tabIndex={0}
              onClick={() => onOpen(l)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpen(l);
                }
              }}
            >
              {rows[i].map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function expiryLabel(expiresAt?: string | null): string {
  if (!expiresAt) return "";
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms < 0) return "expired";
  const h = Math.floor(ms / 3_600_000);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export function ListingsView({
  refreshTrigger,
  navigate,
}: {
  refreshTrigger: number;
  navigate: Navigate;
}) {
  const [data, loading, error] = useInterval(() => api.listings(), 10_000, [refreshTrigger]);
  const listings: Listing[] = data?.listings ?? [];

  const sessions = listings.filter((l) => l.listing_type !== "room");
  const rooms = listings.filter((l) => l.listing_type === "room");

  const open = (l: Listing) => {
    const t = viewForListing(l);
    navigate(t.view, t.opts);
  };

  const sessionRows: RowCells[] = sessions.map((l) => {
    const agentShort = l.agent_id.length > 14 ? `…${l.agent_id.slice(-12)}` : l.agent_id;
    const modLabel = l.module_type?.replace("emporia:", "").replace(/:v\d$/, "") ?? "—";
    const exp = expiryLabel(l.expires_at);
    return [
      // Title + description subtitle
      <div key="title" style={{ minWidth: 160 }}>
        <div style={{ fontWeight: 500, lineHeight: 1.3 }}>{l.title.slice(0, 32)}</div>
        {l.description ? (
          <div style={{ fontSize: 11, opacity: 0.55, marginTop: 2, maxWidth: 220, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>
            {l.description}
          </div>
        ) : null}
      </div>,
      // Module type badge
      <span key="mod" className="e-badge e-badge--dim">{modLabel}</span>,
      // Payment
      l.payment_mode !== "free"
        ? <span key="pay" style={{ color: "var(--theme-focused-foreground)" }}>${l.price_usd}</span>
        : <span key="pay" className="e-faint">free</span>,
      // Agent avatar + name
      <span key="agent" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <AgentMoireAvatar agentId={l.agent_id} size={18} />
        <span className="e-dim" style={{ fontSize: 11 }}>{agentShort}</span>
      </span>,
      // Expiry
      <span key="exp" className="e-faint" style={{ fontSize: 11 }}>{exp}</span>,
    ];
  });

  const roomRows: RowCells[] = rooms.map((r) => [
    r.title.slice(0, 24),
    r.room_type ?? "public",
    r.gate_type ?? "open",
    r.entry_fee_cents ? `$${(r.entry_fee_cents / 100).toFixed(2)}` : "—",
    r.max_members
      ? `${r.member_count ?? 0}/${r.max_members}`
      : String(r.member_count ?? 0),
  ]);

  return (
    <ViewBody>
      <Indent>
        <ViewStatus loading={loading} error={error} empty={listings.length === 0} />
        {listings.length > 0 && (
          <Text className="e-dim e-list-summary">
            {sessions.length} service · {rooms.length} room — click a row to open
          </Text>
        )}
        {sessions.length > 0 && (
          <SlimCard title="Services" action={<Text className="e-dim">{sessions.length}</Text>}>
            <ListingTable
              headers={["LISTING", "TYPE", "PRICE", "AGENT", "EXPIRES"]}
              rows={sessionRows}
              listings={sessions}
              onOpen={open}
            />
          </SlimCard>
        )}
        {rooms.length > 0 && (
          <SlimCard title="Rooms" action={<Text className="e-dim">{rooms.length}</Text>}>
            <ListingTable
              headers={["NAME", "TYPE", "GATE", "FEE", "MBR"]}
              rows={roomRows}
              listings={rooms}
              onOpen={open}
            />
          </SlimCard>
        )}
        {listings.length === 0 && !loading && !error && (
          <Text className="e-faint">{EMPTY_SEED_HINT}</Text>
        )}
      </Indent>
    </ViewBody>
  );
}
