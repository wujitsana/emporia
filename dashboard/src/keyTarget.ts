/** A key pressed on this element is text being typed, not a section shortcut. */
export function isTypingTarget(t: EventTarget | null | undefined): boolean {
  if (typeof HTMLElement === "undefined" || !(t instanceof HTMLElement)) return false;
  return t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement
    || t instanceof HTMLSelectElement || t.isContentEditable === true
    || t.getAttribute("contenteditable") === "true";
}

/**
 * The element that has focus in the tree `inside` belongs to. Inside the Hermes
 * tab the app lives in a *closed* shadow root: a window key listener sees the
 * host element as the target, and composedPath() hides the root's insides from
 * it. The app can still ask its own root node — it holds a node within it.
 */
export function focusedWithin(inside: Node | null | undefined): Element | null {
  const root = inside?.getRootNode?.() as (Document | ShadowRoot | undefined);
  return root && "activeElement" in root ? root.activeElement : null;
}

/** Whether a key event is someone typing: at its target, or at the focus inside the app's own root. */
export function isTyping(e: { target: EventTarget | null }, appNode: Node | null | undefined): boolean {
  return isTypingTarget(e.target) || isTypingTarget(focusedWithin(appNode));
}
