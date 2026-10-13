// One run or call as a list entry, shared by the Prognose card and the station board so both read
// the same way: scheduled · delay · expected in three columns, the day and any note under them,
// and the whole entry a link. Every entry of a kind has one fixed height, whatever it holds.
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Skeleton } from "@/components/ui/skeleton";
import type { Copy } from "@/lib/i18n";
import type { SidePrediction } from "@/lib/tabm";
import { DelayText, ExpectedTime } from "./ModelDelta";
import { icons, type IconKind } from "./icons";
import { clockTime } from "./run-format";
import type { v2Paths } from "./paths";

/** Height of an entry, in every case. A titled entry (the board's) has one more line. */
export const RUN_ROW_HEIGHT = "h-[4.25rem]";
export const RUN_ROW_HEIGHT_TITLED = "h-[5.75rem]";

/** The grid every entry and the column header share: three equal columns and a chevron. */
export const RUN_ROW_GRID =
  "grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_1rem] items-center gap-x-2 sm:gap-x-3";

function ColumnLabel({ kind, children }: { kind: IconKind; children: string }) {
  const Icon = icons[kind];
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Icon className="hidden size-3.5 shrink-0 sm:block" aria-hidden="true" />
      <span className="truncate">{children}</span>
    </span>
  );
}

/** The one header row over a list of entries: scheduled, delay, expected. */
export function RunColumnHeader({ t }: { t: Copy }) {
  return (
    <div
      className={`${RUN_ROW_GRID} border-b border-line px-2 pb-2 text-[11px] uppercase text-fog sm:px-3 sm:text-[12.67px]`}
    >
      <ColumnLabel kind="scheduled">{t.colScheduled}</ColumnLabel>
      <ColumnLabel kind="delay">{t.colDelay}</ColumnLabel>
      <ColumnLabel kind="expected">{t.colExpected}</ColumnLabel>
      <span />
    </div>
  );
}

/**
 * An entry's body, to be placed inside an element that sets its height (RUN_ROW_HEIGHT, or
 * RUN_ROW_HEIGHT_TITLED with `title`).
 *
 * Args:
 *   link: where the entry leads (the train page).
 *   arrival: the scheduled arrival "HH:MM", null for a run that starts here.
 *   departure: the scheduled departure, shown instead of the arrival for a run that starts here.
 *   forecast: the model's forecast for the arrival, once computed.
 *   pending: true while that forecast is still being computed.
 *   day: the run's day, already formatted.
 *   name: a name for the run, shown beside the day (a route of differently numbered trains).
 *   title: a line above the times, for the board's train and destination.
 *   focusable: false for an entry that is only there for the slide animation.
 *   t: the page's translations.
 */
export function RunEntry({
  link,
  arrival,
  departure,
  forecast,
  pending,
  day,
  name,
  title,
  focusable = true,
  t,
}: {
  link: ReturnType<typeof v2Paths.train>;
  arrival: string | null;
  departure: string | null;
  forecast: SidePrediction | undefined;
  pending: boolean;
  day: string;
  name?: string | undefined;
  title?: ReactNode;
  focusable?: boolean;
  t: Copy;
}) {
  // A run that starts here has no arrival to forecast; one that ends here has no onward departure.
  const startsHere = arrival === null && departure !== null;
  const endsHere = departure === null && arrival !== null;
  const StartsIcon = icons.startsHere;
  const EndsIcon = icons.endsHere;
  return (
    <Link
      {...link}
      tabIndex={focusable ? undefined : -1}
      title={startsHere ? t.startsHereWhy : endsHere ? t.endsHereWhy : undefined}
      className={`h-full ${RUN_ROW_GRID} ${
        title ? "grid-rows-3" : "grid-rows-2"
      } content-center gap-y-1 px-2 py-2 hover:bg-ink sm:px-3`}
    >
      {title && <span className="col-span-4 min-w-0">{title}</span>}
      <span className="font-mono text-base text-paper">
        {clockTime(startsHere ? departure : arrival) ?? "—"}
      </span>
      {startsHere ? (
        // Nothing to forecast: the run has no arrival here. Said in the delay and expected
        // columns, which would otherwise hold only dashes.
        <span className="col-span-2 truncate text-[11.5px] text-fog sm:text-[12.67px]">
          {t.noArrivalNoForecast}
        </span>
      ) : (
        <>
          <span className="min-w-0 truncate">
            {forecast !== undefined && arrival ? (
              <DelayText pred={forecast} scheduled={arrival} t={t} />
            ) : pending ? (
              <Skeleton className="h-5 w-16 bg-line" />
            ) : (
              <span className="font-mono text-sm text-fog">—</span>
            )}
          </span>
          <span className="min-w-0 truncate">
            {forecast !== undefined && arrival ? (
              <ExpectedTime pred={forecast} scheduled={arrival} />
            ) : (
              <span className="font-mono text-sm text-fog">—</span>
            )}
          </span>
        </>
      )}
      <ChevronRight className="row-span-2 size-4 self-center text-fog" aria-hidden="true" />
      <span className="truncate text-[12.67px] text-fog">{day}</span>
      <span className="col-span-2 flex min-w-0 items-center gap-2 text-[12.67px]">
        {name && <span className="shrink-0 font-semibold text-paper">{name}</span>}
        {startsHere && (
          <span className="inline-flex min-w-0 items-center gap-1.5 font-semibold text-amber">
            <StartsIcon className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{t.startsHere}</span>
          </span>
        )}
        {endsHere && (
          <span className="inline-flex min-w-0 items-center gap-1.5 font-semibold text-amber">
            <EndsIcon className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{t.endsHere}</span>
          </span>
        )}
      </span>
    </Link>
  );
}
