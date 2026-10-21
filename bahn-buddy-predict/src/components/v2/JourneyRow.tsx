// One journey as a list entry, in three labelled columns (see JourneyColumnHeader): the trip by
// the timetable, the arrival the model forecasts, and the chance of missing the connection.
// Choosing the entry shows that journey; its trains and stations are links of their own, laid
// over the entry.
import { ChevronRight } from "lucide-react";
import { Fragment } from "react";
import { Link } from "@tanstack/react-router";
import type { Journey } from "@/lib/api";
import type { Copy, Locale } from "@/lib/i18n";
import { formatDuration } from "./run-format";
import { changesText, delayLabel } from "./planner-format";
import { MissChanceTag, PlannerExpected } from "./PlannerForecast";
import { icons, type IconKind } from "./icons";
import { v2Paths } from "./paths";

/** A link inside an entry: above the entry's own button, so it is the one that is clicked. */
export const INNER_LINK = "pointer-events-auto relative hover:text-teal hover:underline";

/** Every journey entry is this tall, whatever it shows, so a list of N entries never moves. */
export const JOURNEY_ROW_HEIGHT = "h-[4.5rem]";
/** The grid the entries and their column header share. */
const JOURNEY_GRID =
  "grid grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)_minmax(0,1fr)_0.75rem] items-center gap-x-2 sm:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1fr)_1rem] sm:gap-x-4";

function ColumnLabel({
  kind,
  short,
  children,
}: {
  kind: IconKind;
  short: string;
  children: string;
}) {
  const Icon = icons[kind];
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Icon className="hidden size-3.5 shrink-0 sm:block" aria-hidden="true" />
      <span className="truncate sm:hidden">{short}</span>
      <span className="hidden truncate sm:inline">{children}</span>
    </span>
  );
}

/** The header over a list of journeys, naming what each column is. */
export function JourneyColumnHeader({ t }: { t: Copy }) {
  return (
    <div
      className={`${JOURNEY_GRID} border-b border-line px-2 pb-2 text-[11px] uppercase text-fog sm:px-3 sm:text-[12.67px]`}
    >
      <ColumnLabel kind="scheduled" short={t.colTripShort}>
        {t.colTrip}
      </ColumnLabel>
      <ColumnLabel kind="expected" short={t.colForecastShort}>
        {t.colForecastArrival}
      </ColumnLabel>
      <ColumnLabel kind="change" short={t.colChangeRiskShort}>
        {t.colChangeRisk}
      </ColumnLabel>
      <span />
    </div>
  );
}

/** The stations a journey changes at, each a link to its board at the time of the change. */
export function ChangeStations({
  journey,
  t,
  focusable = true,
}: {
  journey: Journey;
  t: Copy;
  focusable?: boolean;
}) {
  if (journey.changes.length === 0) return <>{t.direct}</>;
  return (
    <>
      {changesText(journey, t)}:{" "}
      {journey.changes.map((change, index) => {
        const arrival = journey.legs[index]?.arrival;
        return (
          <Fragment key={`${change.station.id}-${index}`}>
            {index > 0 && ", "}
            <Link
              {...v2Paths.station(
                change.station.id,
                arrival ? { date: arrival.date, time: arrival.time } : undefined,
              )}
              tabIndex={focusable ? undefined : -1}
              className={INNER_LINK}
            >
              {change.station.name}
            </Link>
          </Fragment>
        );
      })}
    </>
  );
}

/**
 * Args:
 *   journey: the journey shown.
 *   chosen: whether it is the journey shown in full beside the list (a teal bar at its side).
 *   onSelect: choose it.
 *   focusable: false for an entry that is only there as a stand-in while the list loads.
 */
export function JourneyRow({
  journey,
  chosen,
  onSelect,
  focusable = true,
  locale,
  t,
}: {
  journey: Journey;
  chosen: boolean;
  onSelect: () => void;
  focusable?: boolean;
  locale: Locale;
  t: Copy;
}) {
  const duration = formatDuration(journey.duration_min, locale);
  return (
    <div
      className={`relative h-full border-l-2 ${chosen ? "border-teal bg-ink" : "border-transparent"}`}
    >
      <button
        type="button"
        aria-pressed={chosen}
        aria-label={`${journey.departure.time} → ${journey.arrival.time}, ${duration}, ${changesText(journey, t)}`}
        tabIndex={focusable ? undefined : -1}
        onClick={onSelect}
        className="absolute inset-0 hover:bg-ink"
      />
      <div
        className={`pointer-events-none relative h-full ${JOURNEY_GRID} grid-rows-2 content-center gap-y-1 px-2 py-2 sm:px-3`}
      >
        <span className="truncate font-mono text-[13px] tracking-tight text-paper sm:text-base sm:tracking-normal">
          {journey.departure.time}
          <span className="text-fog sm:mx-1">→</span>
          {journey.arrival.time}
        </span>
        <span className="min-w-0 truncate">
          <PlannerExpected
            forecast={journey.forecast}
            scheduled={journey.arrival.time}
            expected={journey.expected_arrival.time}
          />
        </span>
        <span className="min-w-0 truncate text-[13px] sm:text-sm">
          {journey.n_changes === 0 ? (
            <span className="text-fog">{t.noChangeNeeded}</span>
          ) : (
            <MissChanceTag chance={journey.miss_chance} t={t} />
          )}
        </span>
        <ChevronRight className="row-span-2 size-4 self-center text-fog" aria-hidden="true" />
        <span className="flex min-w-0 items-baseline gap-1.5 truncate text-[12.67px] text-fog">
          {journey.legs.map((leg, index) => (
            <Fragment key={`${leg.train_id}-${leg.departure.epoch}`}>
              {index > 0 && <span>›</span>}
              <Link
                {...v2Paths.train(leg.train_id, leg.from.id)}
                tabIndex={focusable ? undefined : -1}
                className={`${INNER_LINK} shrink-0 font-semibold text-paper`}
              >
                {leg.name}
              </Link>
            </Fragment>
          ))}
          <span className="truncate">· {duration}</span>
        </span>
        <span className="min-w-0 truncate text-[12.67px] text-fog">
          {journey.forecast
            ? delayLabel(journey.arrival.epoch, journey.expected_arrival.epoch, t)
            : t.noPrediction}
        </span>
        <span className="min-w-0 truncate text-[12.67px] text-fog">
          <ChangeStations journey={journey} t={t} focusable={focusable} />
        </span>
      </div>
    </div>
  );
}
