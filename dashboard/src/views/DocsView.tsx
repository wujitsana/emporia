/**
 * P171: Emporia's documentation, inside the dashboard. Bundled at build time
 * (see docs.ts), rendered without injecting HTML (see markdown.tsx).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import Text from "@components/Text";
import SidebarLayout from "@components/SidebarLayout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ViewBody } from "../ui/layout";
import { DOCS, resolveDocLink, searchDocs } from "../docs";
import { renderMarkdown } from "../markdown";

/** `#/docs?doc=payments` — so a document can be linked to and bookmarked. */
function docFromHash(): string | null {
  const query = window.location.hash.split("?")[1] ?? "";
  return new URLSearchParams(query).get("doc");
}

export function DocsView({ initialDocId }: { initialDocId?: string | null }) {
  const [docId, setDocId] = useState<string>(() => {
    const wanted = initialDocId ?? docFromHash();
    return wanted && DOCS.some((d) => d.id === wanted) ? wanted : DOCS[0].id;
  });
  useEffect(() => {
    const next = `#/docs?doc=${docId}`;
    if (window.location.hash !== next) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
    }
  }, [docId]);
  const [query, setQuery] = useState("");
  // A section to scroll to once a newly opened doc has rendered. A ref, not
  // state: clearing it must not re-run the scroll effect (P173: it used to
  // scroll to the section and straight back to the top).
  const pendingAnchor = useRef<string | null>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const doc = DOCS.find((d) => d.id === docId) ?? DOCS[0];
  const shown = useMemo(() => searchDocs(query), [query]);

  const scrollTo = (anchor: string) => {
    const root = mainRef.current;
    const el = root?.querySelector(`[id="${CSS.escape(anchor)}"]`);
    if (el) el.scrollIntoView({ block: "start" });
  };

  // A newly opened doc starts at the top, or at the section a link asked for.
  useEffect(() => {
    const anchor = pendingAnchor.current;
    pendingAnchor.current = null;
    if (anchor) scrollTo(anchor);
    else mainRef.current?.scrollTo?.({ top: 0 });
  }, [docId]);

  const body = useMemo(() => renderMarkdown(doc.text, {
    resolveLink: (href) => resolveDocLink(doc, href),
    onDocLink: (id, anchor) => {
      pendingAnchor.current = anchor ?? null;
      if (id === doc.id) { if (anchor) scrollTo(anchor); } else setDocId(id);
    },
    onAnchor: scrollTo,
  }), [doc]);

  const sidebar = (
    <div className="e-docs-rail">
      <input
        className="e-docs-search"
        type="search"
        placeholder="Search the docs"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label="Search the docs"
      />
      {shown.map((d) => (
        <FlatRailItem key={d.id} selected={d.id === doc.id} onClick={() => setDocId(d.id)}>
          <div className="e-docs-rail-item">
            <Text>{d.title}</Text>
            <Text className="e-faint">{d.blurb}</Text>
          </div>
        </FlatRailItem>
      ))}
      {!shown.length && <Text className="e-faint">Nothing matches “{query}”.</Text>}
    </div>
  );

  return (
    <ViewBody flush toolbar={<Text className="e-faint">{doc.path} · bundled with this dashboard</Text>}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={28}>
        <div className="e-split-main e-doc" ref={mainRef}>
          {body}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
