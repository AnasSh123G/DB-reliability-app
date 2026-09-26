// The model's own delay figure, shown next to (never merged with) the
// average -- see openspec/changes/add-browser-delay-predictions/design.md D4.
// One decimal place, unlike the average's whole minutes, is a deliberate,
// small visual cue that the two numbers come from different sources.
import { delayColor } from "@/lib/delay";
import type { SidePrediction } from "@/lib/tabm";

// Language-neutral: this component has no locale, so the explanation is bilingual.
const RULE_TITLE: Record<SidePrediction["rule"], string> = {
  window:
    "Centre of the most likely ±3-minute window (not the median) · " +
    "Mitte des wahrscheinlichsten ±3-Minuten-Fensters (nicht der Median)",
  median: "Median of the model's forecast · Median der Modellprognose",
};

export function ModelDelta({ pred }: { pred: SidePrediction }) {
  const sign = pred.point_min >= 0 ? "+" : "";
  return (
    <span
      className={`block font-mono text-[10px] font-semibold ${delayColor(pred.point_min)}`}
      title={`${RULE_TITLE[pred.rule]} · p25–p75: ${pred.p25_min.toFixed(1)} … ${pred.p75_min.toFixed(1)} min`}
    >
      <span className="mr-1 font-normal text-fog">ML</span>
      {sign}
      {pred.point_min.toFixed(1)} min
    </span>
  );
}
