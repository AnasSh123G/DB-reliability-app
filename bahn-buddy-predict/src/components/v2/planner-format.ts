import type { Journey } from "@/lib/api";
import type { Copy } from "@/lib/i18n";
export function changesText(journey: Journey, t: Copy) {
  return journey.n_changes === 0
    ? t.direct
    : journey.n_changes === 1
      ? t.oneChange
      : t.nChanges.replace("{n}", String(journey.n_changes));
}
export function sameJourney(a: Journey, b: Journey) {
  return (
    a.legs.length === b.legs.length &&
    a.legs.every(
      (leg, index) =>
        leg.train_id === b.legs[index]?.train_id &&
        leg.departure.epoch === b.legs[index]?.departure.epoch,
    )
  );
}

/** "+22 min" or "on time", from the scheduled and expected epochs. */
export function delayLabel(scheduledEpoch: number, expectedEpoch: number, t: Copy) {
  const minutes = Math.round((expectedEpoch - scheduledEpoch) / 60);
  return minutes > 0 ? t.minutesLate.replace("{n}", String(minutes)) : t.arrivesOnTime;
}
