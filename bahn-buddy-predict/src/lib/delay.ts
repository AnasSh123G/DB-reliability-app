import { addMinutes } from "@/lib/prediction";
import type { DelayPred } from "@/lib/api";

// The one delay-severity rule used everywhere in the app: <6min teal,
// 6-14min amber, >=15min danger.
export function delayColor(minutes: number): string {
  if (minutes >= 15) return "text-danger";
  if (minutes >= 6) return "text-amber";
  return "text-teal";
}

// scheduled + the historical mean delay, or null when there's no average to
// offset by -- never fabricate a time from a missing prediction.
export function expectedTime(scheduled: string | null, pred: DelayPred | null): string | null {
  if (!scheduled || !pred) return null;
  return addMinutes(scheduled, pred.p50_min);
}
