import ActionButton from "@components/ActionButton";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import React, { useState } from "react";
import { api } from "../api";
import type { EmporiaEvent } from "../api";
import { FlatRailItem } from "../ui/FlatRailItem";
import { useInterval } from "../hooks";
import { EMPTY_SEED_HINT } from "../navigation";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { EmptyPane } from "../ui/cards";

function statusDot(status: string) {
  if (status === "active" || status === "open")
    return <span style={{ color: "var(--theme-focused-foreground)" }}>●</span>;
  if (status === "completed" || status === "closed") return <span className="e-faint">○</span>;
  return <span className="e-dim">◌</span>;
}

function EntryCount({ ev }: { ev: EmporiaEvent }) {
  const fee = parseFloat(ev.entry_fee_usd ?? "0");
  const count = ev.participant_count ?? 0;
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", marginTop: 2 }}>
      {count > 0 ? (
        <span className="e-gate-badge e-gate-badge--invite">{count} entered</span>
      ) : (
        <Text className="e-faint" style={{ fontSize: 11 }}>0 entered</Text>
      )}
      {fee > 0 && (
        <span className="e-gate-badge e-gate-badge--paid">${fee.toFixed(2)} entry</span>
      )}
    </div>
  );
}

function EventDetail({ ev, onBack }: { ev: EmporiaEvent; onBack: () => void }) {
  const module = ev.module_type.replace("emporia:", "").replace(":v1", "");
  const count = ev.participant_count ?? 0;
  const fee = parseFloat(ev.entry_fee_usd ?? "0");

  return (
    <div className="e-split-main e-agora-detail">
      <div className="e-toolbar-h">
        <Text className="e-dim">{module}</Text>
        <ActionButton onClick={onBack}>← events</ActionButton>
      </div>
      <div className="e-agora-detail__scroll">
        <Text className="e-agora-detail__title">{ev.title}</Text>
        <Text className="e-dim e-agora-detail__meta">
          {statusDot(ev.status)} {ev.status} · {new Date(ev.created_at).toLocaleDateString()}
        </Text>
        <br />
        <Divider />
        <br />
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <RowSpaceBetween>
            <Text className="e-dim">Organizer</Text>
            <Text>{ev.organizer_id}</Text>
          </RowSpaceBetween>
          <RowSpaceBetween>
            <Text className="e-dim">Module</Text>
            <Text>{module}</Text>
          </RowSpaceBetween>
          <RowSpaceBetween>
            <Text className="e-dim">Entry fee</Text>
            <Text>{fee > 0 ? `$${fee.toFixed(2)}` : "free"}</Text>
          </RowSpaceBetween>
          <RowSpaceBetween>
            <Text className="e-dim">Participants</Text>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Text style={{ fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
                {count}
              </Text>
              {count > 0 && fee > 0 && (
                <Text className="e-dim" style={{ fontSize: 11 }}>
                  (${(count * fee).toFixed(2)} collected)
                </Text>
              )}
            </div>
          </RowSpaceBetween>
        </div>
        <Text className="e-faint e-agora-readonly-hint">Read-only · agents join via relay</Text>
      </div>
    </div>
  );
}

export function EventsView({ refreshTrigger }: { refreshTrigger: number }) {
  const [data, loading, error] = useInterval(() => api.events(), 10_000, [refreshTrigger]);
  const [selected, setSelected] = useState<EmporiaEvent | null>(null);
  const events: EmporiaEvent[] = data?.events ?? [];

  const sidebar = (
    <div>
      {events.length === 0 && !loading && (
        <Indent><Text className="e-faint">No events.</Text></Indent>
      )}
      {events.map((ev) => (
        <FlatRailItem
          key={ev.event_id}
          selected={selected?.event_id === ev.event_id}
          onClick={() => setSelected(ev)}
        >
          <RowSpaceBetween>
            <Text style={{ fontSize: 13 }}>{ev.title}</Text>
            {statusDot(ev.status)}
          </RowSpaceBetween>
          <EntryCount ev={ev} />
        </FlatRailItem>
      ))}
    </div>
  );

  const main = selected ? (
    <EventDetail ev={selected} onBack={() => setSelected(null)} />
  ) : (
    <Indent>
      {events.length === 0 && !loading && !error ? (
        <EmptyPane
          title="No scheduled events"
          hint={EMPTY_SEED_HINT}
          facts={[["Type", "tournaments & brackets on relay"]]}
        />
      ) : (
        <Text className="e-faint" style={{ padding: "16px 0" }}>
          Select an event to view details.
        </Text>
      )}
    </Indent>
  );

  return (
    <ViewBody flush toolbar={<Text className="e-dim">{events.length} events</Text>}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={28}>
        <div className="e-split-main e-agora-pane">
          <ViewStatus loading={loading} error={error} />
          {main}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
