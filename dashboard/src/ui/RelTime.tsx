import { exactTime, relativeTime } from "../postCardModel";

/** A time in a list: "3m ago", with the exact time on hover (postCardModel.relativeTime). */
export function RelTime({ at, className }: { at: string; className?: string }) {
  if (!at) return null;
  return <time className={className} dateTime={at} title={exactTime(at)}>{relativeTime(at)}</time>;
}
