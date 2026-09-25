// The model's own delay figure, shown next to (never merged with) the
// average -- see openspec/changes/add-browser-delay-predictions/design.md D4.
// One decimal place, unlike the average's whole minutes, is a deliberate,
// small visual cue that the two numbers come from different sources.
import { delayColor } from "@/lib/delay";
import type { SidePrediction } from "@/lib/tabm";

export function ModelDelta({ pred }: { pred: SidePrediction }) {
  const sign = pred.p50_min >= 0 ? "+" : "";
  return (
    <span className={`block font-mono text-[10px] font-semibold ${delayColor(pred.p50_min)}`}>
      <span className="mr-1 font-normal text-fog">TabM</span>
      {sign}
      {pred.p50_min.toFixed(1)} min
    </span>
  );
}
