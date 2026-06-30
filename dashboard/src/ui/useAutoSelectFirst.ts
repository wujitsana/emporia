import { useEffect, useRef } from "react";

/** Select first list item when nothing is selected yet (spectator UX). */
export function useAutoSelectFirst<T>(
  items: T[],
  selected: T | null,
  onSelect: (item: T) => void,
  getKey: (item: T) => string,
  blockKey?: string | null,
) {
  const didAuto = useRef(false);
  useEffect(() => {
    if (blockKey) {
      const hit = items.some((i) => getKey(i) === blockKey);
      if (hit) return;
    }
    if (selected || items.length === 0) return;
    if (didAuto.current) return;
    didAuto.current = true;
    onSelect(items[0]);
  }, [items, selected, blockKey, getKey, onSelect]);
}