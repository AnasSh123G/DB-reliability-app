import { ArrowLeft, MapPin } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { useTrainRoute } from "@/hooks/use-dbrel";
import { delayColor, expectedTime } from "@/lib/delay";
import type { DelayPred } from "@/lib/api";
import { copy, type Locale } from "@/lib/i18n";

interface Props {
  trainId: number;
  locale: Locale;
  bare?: boolean;
  className?: string;
}

const DESKTOP_GRID =
  "hidden sm:grid sm:grid-cols-[minmax(0,1fr)_72px_72px] sm:gap-x-3 md:grid-cols-[minmax(0,1fr)_96px_96px] md:gap-x-4";

function DelayCell({
  scheduled,
  pred,
  noAverageLabel,
}: {
  scheduled: string | null;
  pred: DelayPred | null;
  noAverageLabel: string;
}) {
  const expected = expectedTime(scheduled, pred);
  if (expected && pred) {
    return (
      <>
        <span className={`block font-semibold ${delayColor(pred.p50_min)}`}>{expected}</span>
        <span className={`block text-[12.67px] font-semibold ${delayColor(pred.p50_min)}`}>
          +{pred.p50_min} min
        </span>
      </>
    );
  }
  return <span className="block text-mist">{scheduled ? noAverageLabel : "—"}</span>;
}

export function TrainOverview({ trainId, locale, bare = false, className }: Props) {
  const t = copy[locale];
  const { data: route, isLoading, isError } = useTrainRoute(trainId);

  const wrapperClass = `${bare ? "" : "rise mt-6 rounded-2xl border border-line bg-panel p-5 sm:p-7"} ${className ?? ""}`;

  if (isLoading) {
    return (
      <section className={wrapperClass}>
        <p className="p-4 text-sm text-fog">{t.loading}</p>
      </section>
    );
  }

  if (isError || !route) {
    return (
      <section className={wrapperClass}>
        <p className="p-4 text-sm text-fog">{t.noTrain}</p>
      </section>
    );
  }

  const { train, stops, n_monitored } = route;

  return (
    <section className={wrapperClass} aria-labelledby="train-heading">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-5">
        <div className="flex items-start gap-3">
          {!bare && (
            <Button
              variant="ghost"
              size="icon"
              asChild
              aria-label={t.back}
              className="rounded-xl text-fog hover:bg-ink hover:text-paper"
            >
              <Link to="/">
                <ArrowLeft />
              </Link>
            </Button>
          )}
          <div>
            <p className="text-xs text-fog">
              {t.trainRoute} {train.destination ?? "—"}
            </p>
            <h2 id="train-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
              {train.name}
              {train.replacement_bus && (
                <span className="ml-2 align-middle text-xs font-normal text-amber">
                  {t.replacementBus}
                </span>
              )}
            </h2>
            <p className="mt-1 text-xs text-fog">
              {[train.category, train.operator].filter(Boolean).join(" · ")}
              {" · "}
              {train.n_stops} {t.stops}, {n_monitored} {t.monitored}
            </p>
            {train.line && (
              <Link
                to="/line/$lineId"
                params={{ lineId: String(train.line.id) }}
                className="mt-1 inline-block text-xs text-teal hover:underline"
              >
                {train.line.name}
              </Link>
            )}
          </div>
        </div>
      </div>

      <div className={`mt-5 ${DESKTOP_GRID} text-[12.67px] uppercase text-fog`}>
        <span>{t.stop}</span>
        <span>{t.arrival}</span>
        <span>{t.expectedArrival}</span>
      </div>
      <ol className="mt-2">
        {stops.map((stop) => {
          const nameNode: ReactNode =
            stop.monitored && stop.station_id ? (
              <Link
                to="/station/$stationId"
                params={{ stationId: String(stop.station_id) }}
                className="hover:underline"
              >
                {stop.name}
              </Link>
            ) : (
              stop.name
            );

          return (
            <li key={stop.seq} className="border-t border-line py-4 sm:py-3">
              <div className="flex min-w-0 items-center gap-3 sm:hidden">
                <span className="grid size-6 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                  <MapPin className="size-3.5" />
                </span>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-paper">{nameNode}</p>
                  <p className="text-[12.67px] text-fog">
                    {stop.monitored
                      ? stop.platform
                        ? `${t.platform} ${stop.platform}`
                        : null
                      : t.notMonitored}
                  </p>
                </div>
              </div>

              {stop.monitored ? (
                <>
                  <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-4 sm:hidden">
                    <div>
                      <p className="text-[12.67px] uppercase text-fog">{t.arrival}</p>
                      <p className="mt-1 font-display text-sm text-mist">{stop.arr ?? "—"}</p>
                    </div>
                    <div>
                      <p className="text-[12.67px] uppercase text-fog">{t.expectedArrival}</p>
                      <p className="mt-1 font-display text-sm">
                        <DelayCell
                          scheduled={stop.arr}
                          pred={stop.arr_pred}
                          noAverageLabel={t.noAverage}
                        />
                      </p>
                    </div>
                  </div>

                  <div className={DESKTOP_GRID}>
                    <div className="flex min-w-0 items-center gap-3">
                      <span className="grid size-6 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                        <MapPin className="size-3.5" />
                      </span>
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-paper">{nameNode}</p>
                        {stop.platform && (
                          <p className="text-[12.67px] text-fog">
                            {t.platform} {stop.platform}
                          </p>
                        )}
                      </div>
                    </div>
                    <span className="self-center font-display text-sm text-mist">
                      {stop.arr ?? "—"}
                    </span>
                    <span className="self-center font-display text-sm">
                      <DelayCell
                        scheduled={stop.arr}
                        pred={stop.arr_pred}
                        noAverageLabel={t.noAverage}
                      />
                    </span>
                  </div>
                </>
              ) : (
                <div className={DESKTOP_GRID}>
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="grid size-6 shrink-0 place-items-center rounded-lg bg-ink text-fog">
                      <MapPin className="size-3.5" />
                    </span>
                    <p className="truncate text-sm font-medium text-paper">{nameNode}</p>
                  </div>
                  <span className="col-span-2 self-center text-[13.67px] text-fog">
                    {t.notMonitored}
                  </span>
                </div>
              )}
            </li>
          );
        })}
      </ol>

      {n_monitored === 0 && (
        <p className="mt-4 rounded-xl border border-line bg-ink p-3 text-xs text-fog">
          {t.noMonitoredStops}
        </p>
      )}
    </section>
  );
}
