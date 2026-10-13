// The v2 station board, generated from v1's StationBoard.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md). Its
// calls are the same entries as the Prognose card's (see RunEntry), with a title line for the train.
import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useStationBoard } from "@/hooks/use-dbrel";
import { useModelPredictions } from "@/hooks/use-tabm";
import { copy, type Locale } from "@/lib/i18n";
import { RUN_ROW_HEIGHT_TITLED, RunColumnHeader, RunEntry } from "./RunEntry";
import { icons } from "./icons";
import { TrainNumber } from "./TrainNumber";
import { formatRunDay } from "./run-format";
import { v2Paths } from "./paths";

interface Props {
  stationId: number;
  /** With `time`, the moment to show the board for; without them it is the live board. */
  date?: string | undefined;
  time?: string | undefined;
  locale: Locale;
  /** Told the station's name once the board has it, for the search field. */
  onName?: ((name: string) => void) | undefined;
}

export function BoardV2({ stationId, date, time, locale, onName }: Props) {
  const t = copy[locale];
  const [widened, setWidened] = useState(false);
  const {
    data: board,
    isLoading,
    isError,
  } = useStationBoard(stationId, {
    ...(widened ? { backMin: 120, fwdMin: 240 } : {}),
    ...(date && time ? { date, time } : {}),
  });
  const stationName = board?.station.name;
  useEffect(() => {
    if (stationName) onName?.(stationName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stationName]);
  const predictions = useModelPredictions(
    board?.model_stamp,
    (board?.calls ?? []).map((call) => ({
      key: call.station_stop_id,
      model_input: call.model_input,
    })),
  );

  if (isLoading) {
    return (
      <Card
        className="rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
        aria-label={t.loading}
      >
        <Skeleton className="h-7 w-2/3 bg-line" />
        <Skeleton className="mt-2 h-4 w-1/3 bg-line" />
        <div className="mt-5 space-y-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full bg-line" />
          ))}
        </div>
      </Card>
    );
  }

  if (isError || !board) {
    return (
      <Card className="rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7">
        <p className="text-sm text-fog">{t.error}</p>
      </Card>
    );
  }

  return (
    <Card
      className="rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
      role="region"
      aria-labelledby="board-heading"
    >
      <div className="flex items-start gap-3 border-b border-line pb-5">
        <div>
          <p className="text-xs text-fog">{t.stationBoard}</p>
          <h2 id="board-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
            {board.station.name}
            {board.station.eva && (
              <span className="ml-2 align-middle font-mono text-xs font-normal text-fog">
                EVA {board.station.eva}
              </span>
            )}
          </h2>
          <p className="mt-1 text-xs text-fog">
            <span className="inline-flex items-center gap-1.5">
              <icons.time className="size-3.5 shrink-0" aria-hidden="true" />
              {t.windowLabel}: {board.window.from} – {board.window.to}
            </span>
            <span className="ml-3 inline-flex items-center gap-1.5">
              <icons.date className="size-3.5 shrink-0" aria-hidden="true" />
              {board.window.date}
            </span>
          </p>
          {!board.station.tracked && (
            <Alert className="mt-2 border-line bg-ink">
              <AlertDescription className="text-xs text-amber">
                {t.notTrackedStation}
              </AlertDescription>
            </Alert>
          )}
        </div>
      </div>

      {board.calls.length > 0 ? (
        <div className="mt-3">
          <RunColumnHeader t={t} />
          <ol className="divide-y divide-line">
            {board.calls.map((call) => {
              const forecast = predictions.get(call.station_stop_id)?.arr;
              return (
                <li key={call.station_stop_id} className={RUN_ROW_HEIGHT_TITLED}>
                  <RunEntry
                    link={v2Paths.train(call.train_id, stationId)}
                    arrival={call.arr}
                    departure={call.dep}
                    forecast={forecast}
                    pending={forecast === undefined && call.model_input !== null}
                    day={formatRunDay(call.service_date, locale)}
                    title={
                      <span className="flex min-w-0 items-baseline gap-2">
                        <span className="shrink-0 font-display font-semibold text-paper">
                          {call.train}
                        </span>
                        <TrainNumber name={call.train} numberName={call.number_name} />
                        <span className="truncate text-[13.67px] text-fog">{call.headline}</span>
                      </span>
                    }
                    t={t}
                  />
                </li>
              );
            })}
          </ol>
        </div>
      ) : (
        <div className="px-1 py-8 text-center text-sm text-fog">
          <p>{t.nothingScheduled}</p>
          {board.station.tracked && !widened && (
            <button
              type="button"
              onClick={() => setWidened(true)}
              className="mt-2 text-teal hover:underline"
            >
              ±240 min
            </button>
          )}
        </div>
      )}
    </Card>
  );
}
