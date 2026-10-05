// The model's own delay figure, the only delay shown in the v2 views; the
// historical average is no longer displayed.
import type { Copy } from "@/lib/i18n";
import { addMinutes } from "@/lib/prediction";
import type { SidePrediction } from "@/lib/tabm";

// Language-neutral: the explanation in the tooltip is bilingual.
const RULE_TITLE: Record<SidePrediction["rule"], string> = {
  window:
    "Centre of the most likely ±3-minute window (not the median) · " +
    "Mitte des wahrscheinlichsten ±3-Minuten-Fensters (nicht der Median)",
  median: "Median of the model's forecast · Median der Modellprognose",
};

// "delay of 5 minutes" / "on time" (under a minute, early included), then the
// scheduled time moved by that delay. Whole minutes only; both numbers coloured by the delay.
export function ModelDelta({
  pred,
  scheduled,
  t,
}: {
  pred: SidePrediction;
  scheduled: string;
  t: Copy;
}) {
  const onTime = pred.point_min < 1;
  // "On time" means no shift: 0.6 min rounds to 1, but the arrival time stays the scheduled one.
  const minutes = onTime ? 0 : Math.round(pred.point_min);
  // Up to 5 minutes green, 6-15 amber, over 15 red (on the rounded number shown).
  const tone = minutes <= 5 ? "text-teal" : minutes <= 15 ? "text-amber" : "text-danger";
  const [before, after] = t.delayOf
    .replace("{unit}", minutes === 1 ? t.minuteOne : t.minuteMany)
    .split("{n}");
  const [arrivalBefore, arrivalAfter] = t.expectedArrivalAt.split("{time}");
  return (
    <span
      className="block font-mono text-[12.67px] font-normal text-fog"
      title={`${RULE_TITLE[pred.rule]} · p25–p75: ${Math.round(pred.p25_min)} … ${Math.round(pred.p75_min)} min`}
    >
      <span className="block">
        {onTime ? (
          <span className={`font-semibold ${tone}`}>{t.onTimeNow}</span>
        ) : (
          <>
            {before}
            <span className={`font-semibold ${tone}`}>{minutes}</span>
            {after}
          </>
        )}
      </span>
      <span className="block">
        {arrivalBefore}
        <span className={`font-semibold ${tone}`}>{addMinutes(scheduled, minutes)}</span>
        {arrivalAfter}
      </span>
    </span>
  );
}
