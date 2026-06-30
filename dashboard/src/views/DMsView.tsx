import AlertBanner from "@components/AlertBanner";
import Indent from "@components/Indent";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import { ViewBody } from "../ui/layout";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DMMessage, DMThread } from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ReadOnlyChat } from "../ui/ReadOnlyChat";
import { RELAY, VIEWER } from "../relayEnv";
import { useInterval } from "../hooks";

export function DMsView() {
  const [threads, setThreads] = useState<DMThread[]>([]);
  const [activeThread, setActiveThread] = useState<DMThread | null>(null);
  const [messages, setMessages] = useState<DMMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const loadThreads = useCallback(async () => {
    try {
      const data = await api.dmThreads(VIEWER);
      setThreads(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const loadMessages = useCallback(async (thread: DMThread) => {
    try {
      const data = await api.dmMessages(thread.thread_id, VIEWER);
      setMessages(data.messages);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    if (!activeThread && threads.length > 0) setActiveThread(threads[0]);
  }, [threads, activeThread]);

  useEffect(() => {
    loadThreads();
  }, [loadThreads]);

  useEffect(() => {
    if (!activeThread) return;
    loadMessages(activeThread);
    const iv = setInterval(() => loadMessages(activeThread), 4000);
    return () => clearInterval(iv);
  }, [activeThread, loadMessages]);

  const dmSidebar = (
    <>
      {threads.length === 0 && (
        <Indent>
          <Text className="e-faint">No DM threads on relay.</Text>
        </Indent>
      )}
      {threads.map((t) => (
        <FlatRailItem
          key={t.thread_id}
          selected={activeThread?.thread_id === t.thread_id}
          onClick={() => setActiveThread(t)}
        >
          <div className="e-agent-row">
            <AgentMoireAvatar agentId={t.other_agent} />
            <div>
              <Text>{t.other_agent}</Text>
              <Text className="e-dim">{(t.last_content ?? "—").slice(0, 36)}</Text>
            </div>
          </div>
        </FlatRailItem>
      ))}
    </>
  );

  const chatLines = messages.map((m) => ({
    id: m.message_id,
    sender_id: m.sender_id,
    content: m.content,
    created_at: m.created_at,
  }));

  return (
    <ViewBody
      flush
      toolbar={
        <>
          {activeThread ? <Text>↔ {activeThread.other_agent}</Text> : null}
          <Text className="e-dim">{threads.length}</Text>
        </>
      }
    >
      <SidebarLayout sidebar={dmSidebar} defaultSidebarWidth={28}>
        <div className="e-split-main">
          {error ? <AlertBanner>{error}</AlertBanner> : null}
          {activeThread ? (
            <ReadOnlyChat
              messages={chatLines}
              viewerId={VIEWER}
              endRef={bottomRef}
              footer={
                <Text className="e-faint">Discover · agent DMs (read-only)</Text>
              }
            />
          ) : (
            <Indent>
              <Text className="e-faint">Select a thread — dashboard does not send as an agent.</Text>
            </Indent>
          )}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}