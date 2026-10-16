// The v2 train route view. Its stops are the same entries as the Prognose card's runs and the
// station board's calls (see RunEntry), under the same scheduled · delay · expected header, so a
// run reads the same in the list and in its route. On the lookup page it sits beside the list
// (`embedded`); at /train/<id> it is a page of its own.
import { ArrowLeft, MapPin } from "lucide-react";
import { useEffect, useRef } from "react";
import { Link } from "@tanstack/react-router";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useTrainRoute } from "@/hooks/use-dbrel";
import { useModelPredictions } from "@/hooks/use-tabm";
import { copy, type Locale } from "@/lib/i18n";
import { RUN_ROW_HEIGHT_TITLED, RunColumnHeader, RunEntry, SCROLL_CLASS } from "./RunEntry";
import { TrainNumber } from "./TrainNumber";
import { icons } from "./icons";
import { formatRunDay } from "./run-format";
import { v2Paths } from "./paths";

interface Props {
  trainId: number;
  /** The stop the user came from; the route shown calls there. */
  stationId?: number | undefined;
  /** The run's day (YYYY-MM-DD); its stops are forecast for that day. Without it, for today. */
  date?: string | undefined;
  locale: Locale;
  /**
   * Beside the lookup list: no back button, and the stops scroll inside the card, whose height
   * `className` fixes.
   */
  embedded?: boolean;
  /** Choosing a stop opens its station's board for the run's day and time at that stop. */
  onStop?: ((stop: { stationId: number; date: string; time: string }) => void) | undefined;
  className?: string;
}

/**
 * A service date ('YYYY-MM-DD') as the locale writes it: "12 Aug 2026" or "12.08.2026".
 *
 * @param day - the date, or null when unknown
 * @param locale - the page language
 * @returns the formatted date, or "—" when there is none
 */
function formatDay(day: string | null, locale: Locale): string {
  if (!day) return "—";
  const options: Intl.DateTimeFormatOptions =
    locale === "de"
      ? { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }
      : { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" };
  return new Intl.DateTimeFormat(locale === "de" ? "de-DE" : "en-GB", options).format(
    new Date(`${day}T00:00:00Z`),
  );
}

export function TrainV2({
  trainId,
  stationId,
  date,
  locale,
  embedded = false,
  onStop,
  className,
}: Props) {
  const t = copy[locale];
  const { data: route, isLoading, isError } = useTrainRoute(trainId, stationId, date);
  const predictions = useModelPredictions(
    route?.model_stamp,
    (route?.stops ?? []).map((stop) => ({ key: stop.seq, model_input: stop.model_input })),
  );

  // Bring the stop the user came from into view once the route is on screen: inside the card's
  // own scroll area beside the list, in the page on a page of its own.
  const listRef = useRef<HTMLOListElement>(null);
  const scrolledFor = useRef<unknown>(null);
  useEffect(() => {
    if (!route || stationId === undefined || scrolledFor.current === route) return;
    const row = listRef.current?.querySelector<HTMLElement>("[data-your-stop]");
    const list = listRef.current;
    if (!row || !list) return;
    scrolledFor.current = route;
    if (embedded) list.scrollTop = row.offsetTop - list.clientHeight / 2 + row.offsetHeight / 2;
    else row.scrollIntoView({ block: "center" });
  }, [route, stationId, embedded]);

  const wrapperClass = `rise flex flex-col rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7 ${
    embedded ? "" : "mt-6"
  } ${className ?? ""}`;

  if (isLoading) {
    return (
      <Card className={wrapperClass} aria-label={t.loading}>
        <Skeleton className="h-7 w-2/3 bg-line" />
        <Skeleton className="mt-2 h-4 w-1/3 bg-line" />
        <div className="mt-5 space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className={`${RUN_ROW_HEIGHT_TITLED} w-full bg-line`} />
          ))}
        </div>
      </Card>
    );
  }

  if (isError || !route) {
    return (
      <Card className={wrapperClass}>
        <p className="p-4 text-sm text-fog">{t.noTrain}</p>
      </Card>
    );
  }

  const { train, stops, n_monitored, retired } = route;
  const isYourStop = (stop: (typeof stops)[number]) =>
    stationId !== undefined && stop.station_id === stationId;
  // Under the times: when the train leaves the stop, and from which platform.
  const stopNote = (stop: (typeof stops)[number]) =>
    stop.monitored
      ? [
          stop.dep && stop.arr ? t.departsAt.replace("{time}", stop.dep) : null,
          stop.platform ? t.platformShort.replace("{platform}", stop.platform) : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : t.notMonitored;

  return (
    <Card className={wrapperClass} role="region" aria-labelledby="train-heading">
      <div className="flex shrink-0 items-start gap-3 border-b border-line pb-5">
        {!embedded && (
          <Button
            variant="ghost"
            size="icon"
            asChild
            aria-label={t.back}
            className="rounded-md text-fog hover:bg-ink hover:text-paper"
          >
            <Link {...v2Paths.home()}>
              <ArrowLeft />
            </Link>
          </Button>
        )}
        <div className="min-w-0">
          <p className="text-xs text-fog">
            {t.trainRoute} {train.destination ?? "—"}
          </p>
          <h2 id="train-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
            {train.name}
            <TrainNumber name={train.name} numberName={train.number_name} />
            {train.replacement_bus && (
              <Badge
                variant="outline"
                className="ml-2 align-middle border-amber font-mono text-[12.67px] font-normal text-amber"
              >
                {t.replacementBus}
              </Badge>
            )}
          </h2>
          <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fog">
            {date && (
              <span className="inline-flex items-center gap-1.5">
                <icons.date className="size-3.5 shrink-0" aria-hidden="true" />
                {formatRunDay(date, locale)}
              </span>
            )}
            {!retired && (
              <span>
                {train.n_stops} {t.stops}
              </span>
            )}
            {train.line && (
              <Link {...v2Paths.line(train.line.id)} className="text-teal hover:underline">
                {t.lineNamed.replace("{line}", train.line.name)}
              </Link>
            )}
          </p>
        </div>
      </div>

      {retired && (
        <Alert className="mt-4 border-amber bg-ink">
          <AlertDescription className="text-sm text-paper">
            {t.retiredTrain.replace("{date}", formatDay(retired.last_seen_date, locale))}
          </AlertDescription>
        </Alert>
      )}

      {!retired && stops.length > 0 && (
        <div className="mt-3 flex min-h-0 flex-1 flex-col">
          <RunColumnHeader t={t} chevron={false} />
          <ol
            ref={listRef}
            className={`relative divide-y divide-line ${
              embedded ? `min-h-0 flex-1 overflow-y-auto ${SCROLL_CLASS}` : ""
            }`}
          >
            {stops.map((stop) => {
              const forecast = predictions.get(stop.seq)?.arr;
              const yours = isYourStop(stop);
              const time = stop.arr ?? stop.dep;
              const station = stop.monitored ? stop.station_id : null;
              return (
                <li
                  key={stop.seq}
                  data-your-stop={yours ? "" : undefined}
                  className={RUN_ROW_HEIGHT_TITLED}
                >
                  <RunEntry
                    link={
                      !onStop && station
                        ? v2Paths.station(station, date && time ? { date, time } : undefined)
                        : undefined
                    }
                    onSelect={
                      onStop && station && date && time
                        ? () => onStop({ stationId: station, date, time })
                        : undefined
                    }
                    selected={yours}
                    arrival={stop.monitored ? stop.arr : null}
                    departure={stop.monitored ? stop.dep : null}
                    forecast={stop.monitored ? forecast : undefined}
                    pending={stop.monitored && forecast === undefined && stop.model_input !== null}
                    day={stopNote(stop)}
                    chevron={false}
                    title={
                      <span className="flex min-w-0 items-center gap-2">
                        <MapPin
                          className={`size-3.5 shrink-0 ${stop.monitored ? "text-teal" : "text-fog"}`}
                          aria-hidden="true"
                        />
                        <span
                          className={`truncate font-display font-semibold ${
                            yours ? "text-teal" : "text-paper"
                          }`}
                        >
                          {stop.name}
                        </span>
                      </span>
                    }
                    t={t}
                  />
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {n_monitored === 0 && !retired && (
        <Alert className="mt-4 border-line bg-ink">
          <AlertDescription className="text-xs text-fog">{t.noMonitoredStops}</AlertDescription>
        </Alert>
      )}
    </Card>
  );
}
