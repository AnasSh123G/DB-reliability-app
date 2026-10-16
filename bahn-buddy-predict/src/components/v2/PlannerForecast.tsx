import type { MissChance, PlanForecast } from "@/lib/api";
import type { Copy } from "@/lib/i18n";
import type { SidePrediction } from "@/lib/tabm";
import { delayParts } from "./delay-parts";
import { DelayText } from "./ModelDelta";
import { icons } from "./icons";
/** The planner's forecast as the model's own figure (its median), as the other views show it. */
function planPrediction(forecast: PlanForecast): SidePrediction {
  return { ...forecast, point_min: forecast.p50_min, rule: "median" };
}
export function PlannerDelay({
  forecast,
  scheduled,
  t,
}: {
  forecast: PlanForecast | null;
  scheduled: string;
  t: Copy;
}) {
  return forecast ? (
    <DelayText pred={planPrediction(forecast)} scheduled={scheduled} t={t} />
  ) : (
    <span className="text-sm text-fog">{t.noPrediction}</span>
  );
}
const EXPECTED_TONE = { ok: "text-teal", late: "text-amber", "very-late": "text-danger" } as const;
/** The expected arrival the planner worked out, in the colour of its delay. */
export function PlannerExpected({
  forecast,
  scheduled,
  expected,
}: {
  forecast: PlanForecast | null;
  scheduled: string;
  expected: string;
}) {
  if (!forecast) return <span className="font-mono text-sm text-fog">—</span>;
  const { level } = delayParts(planPrediction(forecast), scheduled);
  return (
    <span className={`font-mono text-sm font-semibold sm:text-base ${EXPECTED_TONE[level]}`}>
      {expected}
    </span>
  );
}
const MISS_TEXT = {
  low: "missLow",
  medium: "missMedium",
  high: "missHigh",
  very_high: "missVeryHigh",
} as const satisfies Record<MissChance, keyof Copy>;
const MISS_TONE = {
  low: "text-teal",
  medium: "text-amber",
  high: "text-danger",
  very_high: "text-danger",
} as const satisfies Record<MissChance, string>;
/** The chance of missing a change, as a coloured band ("under 25%"). */
export function MissChanceTag({ chance, t }: { chance: MissChance | null; t: Copy }) {
  if (!chance) return <span className="text-fog">{t.missUnknown}</span>;
  const Icon = chance === "low" ? icons.change : icons.risk;
  return (
    <span className={`inline-flex items-center gap-1 font-semibold ${MISS_TONE[chance]}`}>
      <Icon className="hidden size-3.5 shrink-0 sm:block" aria-hidden="true" />
      {t[MISS_TEXT[chance]]}
    </span>
  );
}
