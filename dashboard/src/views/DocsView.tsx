/**
 * P171: Emporia's documentation, inside the dashboard. Bundled at build time
 * (see docs.ts), rendered without injecting HTML (see markdown.tsx).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import Text from "@kit/Text";
import SidebarLayout from "@kit/SidebarLayout";
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
  // The open doc, plus the section a link asked for (scrolled to once the doc
  // has rendered). One state value, so the scroll effect runs once per
  // navigation and never because the anchor was cleared (P173: it used to
  // scroll to the section and straight back to the top).
  const [nav, setNav] = useState<{ id: string; anchor: string | null }>(() => {
    const wanted = initialDocId ?? docFromHash();
    return { id: wanted && DOCS.some((d) => d.id === wanted) ? wanted : DOCS[0].id, anchor: null };
  });
  const docId = nav.id;
  const openDoc = (id: string) => setNav((cur) => (cur.id === id ? cur : { id, anchor: null }));
  useEffect(() => {
    const next = `#/docs?doc=${docId}`;
    if (window.location.hash !== next) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
    }
  }, [docId]);
  const [query, setQuery] = useState("");
  // State, not a ref object: the scroll helpers are handed to the markdown
  // renderer inside a memo, and need the element as a value.
  const [mainEl, setMainEl] = useState<HTMLDivElement | null>(null);
  const doc = DOCS.find((d) => d.id === docId) ?? DOCS[0];
  const shown = useMemo(() => searchDocs(query), [query]);

  const scrollTo = useCallback((anchor: string) => {
    const el = mainEl?.querySelector(`[id="${CSS.escape(anchor)}"]`);
    if (el) el.scrollIntoView({ block: "start" });
  }, [mainEl]);

  // A newly opened doc starts at the top, or at the section a link asked for.
  useEffect(() => {
    if (nav.anchor) scrollTo(nav.anchor);
    else mainEl?.scrollTo?.({ top: 0 });
  }, [nav.id, nav.anchor, mainEl, scrollTo]);

  const body = useMemo(() => renderMarkdown(doc.text, {
    resolveLink: (href) => resolveDocLink(doc, href),
    onDocLink: (id, anchor) => {
      if (id === doc.id) { if (anchor) scrollTo(anchor); } else setNav({ id, anchor: anchor ?? null });
    },
    onAnchor: scrollTo,
  }), [doc, scrollTo]);

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
        <FlatRailItem key={d.id} selected={d.id === doc.id} onClick={() => openDoc(d.id)}>
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
        <div className="e-split-main e-doc" ref={setMainEl}>
          {body}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
