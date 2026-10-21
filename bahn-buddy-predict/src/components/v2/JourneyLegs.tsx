// The chosen journey train by train, as a timeline: each train with its departure and arrival by
// the timetable and its arrival by the forecast, and between two trains the change, with the time
// there is to make it and the chance of missing it. Trains lead to their train page, stations to
// their boards at the times the journey calls there.
import { ChevronRight } from "lucide-react";
import { Fragment } from "react";
import { Link } from "@tanstack/react-router";
import { Card } from "@/components/ui/card";
import type { Journey, PlanLeg } from "@/lib/api";
import type { Copy, Locale } from "@/lib/i18n";
import { TrainNumber } from "./TrainNumber";
import { v2Paths } from "./paths";
import { icons } from "./icons";
import { formatDuration, formatRunDay } from "./run-format";
import { changesText, delayLabel } from "./planner-format";
import { MissChanceTag, PlannerExpected } from "./PlannerForecast";

const STATION_LINK = "text-paper hover:text-teal hover:underline";

function Stop({
  kind,
  time,
  station,
  moment,
  forecast,
  t,
}: {
  kind: "departure" | "arrival";
  time: string;
  station: PlanLeg["from"];
  moment: { date: string; time: string };
  forecast?: { leg: PlanLeg } | undefined;
  t: Copy;
}) {
  const Icon = icons[kind];
  return (
    <div className="grid grid-cols-[3.25rem_minmax(0,1fr)] items-baseline gap-x-3 sm:grid-cols-[3.25rem_minmax(0,1fr)_auto] lg:grid-cols-[3.25rem_minmax(0,1fr)]">
      <span className="font-mono text-base text-paper">{time}</span>
      <span className="flex min-w-0 items-center gap-1.5 truncate text-sm">
        <Icon className="size-3.5 shrink-0 text-fog" aria-hidden="true" />
        <Link {...v2Paths.station(station.id, moment)} className={`truncate ${STATION_LINK}`}>
          {station.name}
        </Link>
      </span>
      {forecast ? (
        <span className="col-start-2 text-xs text-fog sm:col-start-auto sm:text-right lg:col-start-2 lg:text-left">
          {t.forecastShort}{" "}
          <PlannerExpected
            forecast={forecast.leg.forecast}
            scheduled={forecast.leg.arrival.time}
            expected={forecast.leg.expected_arrival.time}
          />
          {forecast.leg.forecast && (
            <span className="ml-1.5">
              ({delayLabel(forecast.leg.arrival.epoch, forecast.leg.expected_arrival.epoch, t)})
            </span>
          )}
        </span>
      ) : null}
    </div>
  );
}

export function JourneyLegs({ journey, locale, t }: { journey: Journey; locale: Locale; t: Copy }) {
  const Change = icons.change;
  const first = journey.legs[0];
  const last = journey.legs[journey.legs.length - 1];
  return (
    <Card
      className="rise rounded-lg border border-line bg-panel p-5 text-paper shadow-none"
      role="region"
      aria-labelledby="legs-heading"
    >
      <div className="border-b border-line pb-5">
        <p className="text-xs text-fog">{t.legsTitle}</p>
        <h2 id="legs-heading" className="mt-1 font-display text-xl font-semibold text-paper">
          {first?.from.name} → {last?.to.name}
        </h2>
        <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fog">
          <span className="inline-flex items-center gap-1.5">
            <icons.date className="size-3.5 shrink-0" aria-hidden="true" />
            {formatRunDay(journey.departure.date, locale)}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <icons.scheduled className="size-3.5 shrink-0" aria-hidden="true" />
            {t.scheduledShort} {journey.departure.time} → {journey.arrival.time}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <icons.expected className="size-3.5 shrink-0" aria-hidden="true" />
            {t.forecastShort} {journey.expected_arrival.time}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <icons.duration className="size-3.5 shrink-0" aria-hidden="true" />
            {formatDuration(journey.duration_min, locale)}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Change className="size-3.5 shrink-0" aria-hidden="true" />
            {changesText(journey, t)}
          </span>
        </p>
        <p className="mt-2 text-xs text-fog">{t.legsHint}</p>
      </div>
      <ol className="mt-4 space-y-3">
        {journey.legs.map((leg, index) => {
          const change = journey.changes[index];
          return (
            <Fragment key={`${leg.train_id}-${leg.departure.epoch}`}>
              <li className="rounded-md border border-line bg-ink/40 p-3 sm:p-4">
                <Link
                  {...v2Paths.train(leg.train_id, leg.from.id)}
                  className="group mb-2 flex min-w-0 items-baseline gap-2"
                >
                  <icons.train
                    className="size-4 shrink-0 self-center text-fog"
                    aria-hidden="true"
                  />
                  <span className="shrink-0 font-display font-semibold text-paper group-hover:text-teal group-hover:underline">
                    {leg.name}
                  </span>
                  <span className="shrink-0 whitespace-nowrap">
                    <TrainNumber name={leg.name} numberName={leg.number_name} />
                  </span>
                  <span className="truncate text-[13px] text-fog">
                    {t.toStationShort.replace("{station}", leg.to.name)}
                  </span>
                  <ChevronRight
                    className="ml-auto size-4 shrink-0 self-center text-fog"
                    aria-hidden="true"
                  />
                </Link>
                <div className="space-y-1.5 border-l-2 border-line pl-3">
                  <Stop
                    kind="departure"
                    time={leg.departure.time}
                    station={leg.from}
                    moment={leg.departure}
                    t={t}
                  />
                  <Stop
                    kind="arrival"
                    time={leg.arrival.time}
                    station={leg.to}
                    moment={leg.arrival}
                    forecast={{ leg }}
                    t={t}
                  />
                </div>
              </li>
              {change && (
                <li className="flex flex-col gap-1 rounded-md border border-dashed border-line px-3 py-2.5 text-[13px] sm:flex-row sm:items-center sm:justify-between sm:gap-4 lg:flex-col lg:items-start lg:justify-start lg:gap-1">
                  <span className="flex min-w-0 items-center gap-2 text-fog">
                    <Change className="size-4 shrink-0" aria-hidden="true" />
                    <span className="min-w-0">
                      {t.changeAt.split("{station}")[0]}
                      <Link
                        {...v2Paths.station(change.station.id, leg.arrival)}
                        className={STATION_LINK}
                      >
                        {change.station.name}
                      </Link>
                      {t.changeAt.split("{station}")[1]}
                      <span className="block text-[12.67px]">
                        {t.changeTime
                          .replace("{planned}", String(change.planned_min))
                          .replace("{expected}", String(change.left_min))}
                      </span>
                    </span>
                  </span>
                  <span className="shrink-0 pl-6 sm:pl-0 sm:text-right lg:pl-6 lg:text-left">
                    <span className="block text-[11px] uppercase text-fog">
                      {t.missChanceLabel}
                    </span>
                    <MissChanceTag chance={change.miss_chance} t={t} />
                  </span>
                </li>
              )}
            </Fragment>
          );
        })}
      </ol>
    </Card>
  );
}
