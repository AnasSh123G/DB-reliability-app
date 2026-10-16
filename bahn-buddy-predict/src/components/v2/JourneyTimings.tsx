// This route at other times: always ROWS entries at once, in a box of fixed height, so choosing
// another connection or stepping Earlier/Later reloads the entries in place and the page under
// them never moves. Runs are fetched in two-hour-either-side windows, ahead of the entries shown.
import { ChevronDown, ChevronUp } from "lucide-react";
import { useEffect, useState } from "react";
import { usePlanTimingWindows } from "@/hooks/use-dbrel";
import type { Journey } from "@/lib/api";
import type { Copy, Locale } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { berlinMoment, formatRunDay } from "./run-format";
import { sameJourney } from "./planner-format";
import { JOURNEY_ROW_HEIGHT, JourneyColumnHeader, JourneyRow } from "./JourneyRow";
import { JourneyLegs } from "./JourneyLegs";

/** Entries shown at once, and how far one Earlier/Later press moves. */
const ROWS = 5;
/** Half a timings window (TIMINGS_HALF_WINDOW_S on the server). */
const HALF_WINDOW_S = 2 * 3600;
/** Runs are fetched no further than this from the chosen one. */
const MAX_REACH_S = 24 * 3600;

interface View {
  route: string;
  origin: number;
  /** Epochs the fetched windows are centred on. */
  centres: number[];
  /** Departure epoch of the first entry shown; null centres the chosen journey. */
  first: number | null;
  /** The run picked in this list, shown in full below it. */
  picked: Journey | null;
}

function startView(journey: Journey): View {
  return {
    route: journey.route_key,
    origin: journey.departure.epoch,
    centres: [journey.departure.epoch],
    first: null,
    picked: null,
  };
}

export function JourneyTimings({
  journey,
  from,
  to,
  locale,
  t,
}: {
  journey: Journey;
  from: number;
  to: number;
  locale: Locale;
  t: Copy;
}) {
  const [view, setView] = useState(() => startView(journey));
  // Another connection was chosen: start over in place rather than remounting the card.
  const current =
    view.route === journey.route_key && view.origin === journey.departure.epoch
      ? view
      : startView(journey);
  if (current !== view) setView(current);

  const { instances, isPending, isError } = usePlanTimingWindows(
    { from, to, route: current.route },
    current.centres,
    berlinMoment,
  );

  let start: number;
  if (current.first === null) {
    const own = instances.findIndex((instance) => sameJourney(instance, journey));
    const near = own >= 0 ? own : instances.findIndex((i) => i.departure.epoch >= current.origin);
    start = Math.max(0, Math.min((near < 0 ? 0 : near) - 2, instances.length - ROWS));
  } else {
    const index = instances.findIndex((i) => i.departure.epoch >= current.first!);
    start = index < 0 ? Math.max(0, instances.length - ROWS) : index;
  }
  const shown = instances.slice(start, start + ROWS);
  const chosen =
    (current.picked && shown.find((i) => sameJourney(i, current.picked!))) ??
    shown.find((i) => sameJourney(i, journey)) ??
    current.picked ??
    journey;

  // Keep a page of runs loaded on both sides of the entries shown, one window at a time.
  const lo = Math.min(...current.centres) - HALF_WINDOW_S;
  const hi = Math.max(...current.centres) + HALF_WINDOW_S;
  const after = instances.length - (start + shown.length);
  useEffect(() => {
    if (isPending || isError) return;
    const centre =
      after < ROWS && hi < current.origin + MAX_REACH_S
        ? hi + HALF_WINDOW_S
        : start < ROWS && lo > current.origin - MAX_REACH_S
          ? lo - HALF_WINDOW_S
          : null;
    if (centre !== null)
      setView((previous) =>
        previous.route === current.route && !previous.centres.includes(centre)
          ? { ...previous, centres: [...previous.centres, centre] }
          : previous,
      );
  }, [isPending, isError, after, start, hi, lo, current.origin, current.route]);

  const canEarlier = start > 0;
  const canLater = start + ROWS < instances.length;
  const step = (later: boolean) => {
    const target = later ? instances[start + ROWS] : instances[Math.max(0, start - ROWS)];
    if (target) setView((previous) => ({ ...previous, first: target.departure.epoch }));
  };

  const slots = Array.from({ length: ROWS }, (_, index) => shown[index]);
  const firstLoad = isPending && instances.length === 0;
  return (
    <div className="space-y-5">
      <Card
        className="rise rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
        role="region"
        aria-labelledby="timings-heading"
      >
        <div className="border-b border-line pb-5">
          <p className="text-xs text-fog">{t.otherTimes}</p>
          <h2
            id="timings-heading"
            className="mt-1 truncate font-display text-2xl font-semibold text-paper"
          >
            {journey.legs.map((leg) => leg.name).join(" › ")}
          </h2>
          <p className="mt-1 text-xs text-fog">{t.otherTimesHint}</p>
        </div>
        <div className="pt-3 sm:flex sm:items-stretch sm:gap-5">
          <div className="min-w-0 flex-1">
            <JourneyColumnHeader t={t} />
            <div className="relative" aria-busy={isPending}>
              <ol className="divide-y divide-line">
                {slots.map((instance, index) => (
                  <li
                    key={instance ? instanceKey(instance) : `slot-${index}`}
                    className={JOURNEY_ROW_HEIGHT}
                  >
                    {instance ? (
                      <JourneyRow
                        journey={instance}
                        chosen={sameJourney(instance, chosen)}
                        marked={sameJourney(instance, journey)}
                        onSelect={() => setView((previous) => ({ ...previous, picked: instance }))}
                        day={formatRunDay(instance.departure.date, locale)}
                        locale={locale}
                        t={t}
                      />
                    ) : firstLoad || (isPending && !isError) ? (
                      <div className="h-full px-2 py-2 sm:px-3">
                        <Skeleton className="h-full w-full bg-line" aria-label={t.loading} />
                      </div>
                    ) : null}
                  </li>
                ))}
              </ol>
              {!isPending && (isError || instances.length === 0) && (
                <p
                  role={isError ? "alert" : undefined}
                  className={`absolute inset-0 grid place-items-center px-1 text-center text-sm ${isError ? "text-danger" : "text-fog"}`}
                >
                  {isError ? t.error : t.noTimings}
                </p>
              )}
            </div>
          </div>
          <div className="mt-3 flex gap-3 border-t border-line pt-4 sm:mt-0 sm:w-36 sm:flex-col sm:justify-center sm:gap-3 sm:border-t-0 sm:border-l sm:pt-0 sm:pl-5">
            <Button
              variant="outline"
              onClick={() => step(false)}
              disabled={!canEarlier}
              className="h-[42px] flex-1 gap-2 border-line bg-ink text-paper hover:bg-panel disabled:opacity-40 sm:aspect-square sm:h-auto sm:w-full sm:flex-none sm:flex-col sm:justify-center sm:gap-2"
            >
              <ChevronUp className="size-4" />
              {t.showEarlier}
            </Button>
            <Button
              variant="outline"
              onClick={() => step(true)}
              disabled={!canLater}
              className="h-[42px] flex-1 gap-2 border-line bg-ink text-paper hover:bg-panel disabled:opacity-40 sm:aspect-square sm:h-auto sm:w-full sm:flex-none sm:flex-col sm:justify-center sm:gap-2"
            >
              <ChevronDown className="size-4" />
              {t.showLater}
            </Button>
          </div>
        </div>
      </Card>
      <JourneyLegs journey={chosen} locale={locale} t={t} />
    </div>
  );
}

const instanceKey = (journey: Journey) =>
  `${journey.departure.epoch}-${journey.legs.map((leg) => leg.train_id).join("-")}`;
