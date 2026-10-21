import type { Journey } from "@/lib/api";
import type { Copy } from "@/lib/i18n";
import { addDays } from "./run-format";

/**
 * A date and clock time moved by some minutes, on the wall clock.
 *
 * @param date - YYYY-MM-DD
 * @param time - HH:MM
 * @param minutes - how far to move it, negative for earlier
 * @returns the new date and time
 */
export function shiftMoment(date: string, time: string, minutes: number) {
  const [hours = 0, mins = 0] = time.split(":").map(Number);
  const total = hours * 60 + mins + minutes;
  const rest = ((total % 1440) + 1440) % 1440;
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: addDays(date, Math.floor(total / 1440)),
    time: `${pad(Math.floor(rest / 60))}:${pad(rest % 60)}`,
  };
}
export function changesText(journey: Journey, t: Copy) {
  return journey.n_changes === 0
    ? t.direct
    : journey.n_changes === 1
      ? t.oneChange
      : t.nChanges.replace("{n}", String(journey.n_changes));
}
/** "+22 min" or "on time", from the scheduled and expected epochs. */
export function delayLabel(scheduledEpoch: number, expectedEpoch: number, t: Copy) {
  const minutes = Math.round((expectedEpoch - scheduledEpoch) / 60);
  return minutes > 0 ? t.minutesLate.replace("{n}", String(minutes)) : t.arrivesOnTime;
}
