// The model's own delay figure, the only delay shown in the v2 views; the
// historical average is no longer displayed.
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
