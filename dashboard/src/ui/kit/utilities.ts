// The few helpers the kit's components use, from srcl's common/utilities.ts (MIT, see LICENSE-srcl).

type ClassValue = string | number | null | undefined | false | ClassValue[] | Record<string, unknown>;

export function classNames(...args: ClassValue[]): string {
  const out: string[] = [];
  for (const arg of args) {
    if (!arg) continue;
    if (typeof arg === "string" || typeof arg === "number") out.push(String(arg));
    else if (Array.isArray(arg)) {
      const inner = classNames(...arg);
      if (inner) out.push(inner);
    } else if (typeof arg === "object") {
      for (const [key, on] of Object.entries(arg)) if (on) out.push(key);
    }
  }
  return out.join(" ");
}

export function isEmpty(text: unknown): boolean {
  if (text === 0) return false; // zero is a value
  if (!text || typeof text === "object") return true;
  return !String(text).trim();
}

const FOCUSABLE = ["a[href]", "button", "input", "select", "textarea", '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]'].join(", ");

export function findNextFocusable(element: Element | null, direction: "next" | "previous" = "next"): HTMLElement | null {
  if (!element) return null;
  const all = Array.from(document.querySelectorAll<HTMLElement>(FOCUSABLE));
  const i = all.indexOf(element as HTMLElement);
  if (i === -1) return null;
  return all[direction === "next" ? (i + 1) % all.length : (i - 1 + all.length) % all.length];
}
