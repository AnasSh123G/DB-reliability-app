// The model's own delay figure, the only delay shown in the v2 views; the
// historical average is no longer displayed.
import { CircleCheck, OctagonAlert, TriangleAlert } from "lucide-react";
import type { Copy } from "@/lib/i18n";
import { addMinutes } from "@/lib/prediction";
import { delayParts } from "./delay-parts";
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

const TONES = {
  ok: { text: "text-teal", Icon: CircleCheck },
  late: { text: "text-amber", Icon: TriangleAlert },
  "very-late": { text: "text-danger", Icon: OctagonAlert },
} as const;

/**
 * The forecast delay written out as "+4 min" or "on time", with an icon for how bad it is, so the
 * colour is never the only way to read it.
 *
 * Args:
 *   pred: the model's forecast for the call.
 *   scheduled: the call's scheduled time, "HH:MM".
 *   t: the page's translations.
 */
export function DelayText({
  pred,
  scheduled,
  t,
}: {
  pred: SidePrediction;
  scheduled: string;
  t: Copy;
}) {
  const { minutes, onTime, level } = delayParts(pred, scheduled);
  const { text, Icon } = TONES[level];
  return (
    <span
      className={`inline-flex items-center gap-1 font-mono text-sm font-semibold ${text}`}
      title={`${RULE_TITLE[pred.rule]} · p25–p75: ${Math.round(pred.p25_min)} … ${Math.round(pred.p75_min)} min`}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      {onTime ? t.onTimeNow : t.delayMinutes.replace("{n}", String(minutes))}
    </span>
  );
}

/** The expected arrival time, in the colour of its delay. */
export function ExpectedTime({ pred, scheduled }: { pred: SidePrediction; scheduled: string }) {
  const { level, expected } = delayParts(pred, scheduled);
  return <span className={`font-mono text-sm font-semibold ${TONES[level].text}`}>{expected}</span>;
}
