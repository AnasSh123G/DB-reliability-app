import { addMinutes } from "@/lib/prediction";
import type { SidePrediction } from "@/lib/tabm";

/**
 * What a forecast says, in the numbers the page shows.
 *
 * Args:
 *   pred: the model's forecast for a call.
 *   scheduled: the call's scheduled time, "HH:MM".
 *
 * Returns: the delay in whole minutes (0 when under a minute, early included), whether that
 *   counts as on time, the tone class for the delay (up to 5 minutes green, 6-15 amber, over 15
 *   red), and the scheduled time moved by the delay.
 */
export function delayParts(pred: SidePrediction, scheduled: string) {
  const onTime = pred.point_min < 1;
  const minutes = onTime ? 0 : Math.round(pred.point_min);
  const level = minutes <= 5 ? "ok" : minutes <= 15 ? "late" : "very-late";
  return { minutes, onTime, level, expected: addMinutes(scheduled, minutes) } as const;
}
