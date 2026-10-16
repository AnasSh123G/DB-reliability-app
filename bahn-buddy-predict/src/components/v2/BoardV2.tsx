// The v2 station board, generated from v1's StationBoard.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md). Its
// calls are the same entries as the Prognose card's (see RunEntry), with a title line for the train.
import { ArrowLeft } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useStationBoard } from "@/hooks/use-dbrel";
import { useModelPredictions } from "@/hooks/use-tabm";
import { copy, type Locale } from "@/lib/i18n";
import { RUN_ROW_HEIGHT_TITLED, RunColumnHeader, RunEntry, SCROLL_CLASS } from "./RunEntry";
import { icons } from "./icons";
import { TrainNumber } from "./TrainNumber";
import { clockTime, formatRunDay } from "./run-format";
import { v2Paths } from "./paths";
import type { RunChoice } from "./run-format";

interface Props {
  stationId: number;
  /** With `time`, the moment to show the board for; without them it is the live board. */
  date?: string | undefined;
  time?: string | undefined;
  locale: Locale;
  /** Told the station's name once the board has it, for the search field. */
  onName?: ((name: string) => void) | undefined;
  /** The call whose route is shown beside the board; choosing a call calls `onChoose`. */
  chosen?: RunChoice | null | undefined;
  onChoose?: ((choice: RunChoice, byUser: boolean) => void) | undefined;
  /**
   * On the lookup page: no space above the card, and the calls scroll inside it, whose height
   * `className` fixes, starting at the first call at or after the board's moment.
   */
  embedded?: boolean;
  /** Shown as a back button in the header: return to what the board replaced. */
  onBack?: (() => void) | undefined;
  /** The run the user came from (a stop of its route): its call is marked and scrolled to. */
  fromRun?: { trainId: number; date: string } | undefined;
  className?: string;
}

/** Berlin's date and time now, "YYYY-MM-DD HH:MM", to find the first call still to come. */
function berlinNow() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
}

export function BoardV2({
  stationId,
  date,
  time,
  locale,
  onName,
  chosen,
  onChoose,
  embedded = false,
  onBack,
  fromRun,
  className,
}: Props) {
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
  // The first call at or after the board's moment: chosen when nothing is yet, so the route beside
  // the board is never empty, and where the calls start when they scroll inside the card.
  const calls = board?.calls;
  const from = date && time ? `${date} ${time}` : berlinNow();
  const first = calls?.length
    ? (calls.find(
        (call) => `${call.service_date} ${clockTime(call.dep ?? call.arr) ?? ""}` >= from,
      ) ?? calls[0]!)
    : undefined;
  const isFromRun = (call: NonNullable<typeof calls>[number]) =>
    fromRun !== undefined &&
    call.train_id === fromRun.trainId &&
    call.service_date === fromRun.date;
  const start = calls?.find(isFromRun) ?? first;
  useEffect(() => {
    if (!onChoose || chosen || !first) return;
    onChoose(
      {
        key: `call-${first.station_stop_id}`,
        trainId: first.train_id,
        date: first.service_date,
        stationId,
      },
      false,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calls, chosen]);
  const listRef = useRef<HTMLOListElement>(null);
  const scrolledFor = useRef<unknown>(null);
  useEffect(() => {
    const list = listRef.current;
    if (!embedded || !calls || !list || scrolledFor.current === calls) return;
    scrolledFor.current = calls;
    const row = list.querySelector<HTMLElement>("[data-start]");
    list.scrollTop = row?.offsetTop ?? 0;
  }, [calls, embedded]);
  const predictions = useModelPredictions(
    board?.model_stamp,
    (board?.calls ?? []).map((call) => ({
      key: call.station_stop_id,
      model_input: call.model_input,
    })),
  );

  const cardClass = `rise flex flex-col rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7 ${
    embedded ? "" : "mt-6"
  } ${className ?? ""}`;

  if (isLoading) {
    return (
      <Card className={cardClass} aria-label={t.loading}>
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
      <Card className={cardClass}>
        <p className="text-sm text-fog">{t.error}</p>
      </Card>
    );
  }

  return (
    <Card className={cardClass} role="region" aria-labelledby="board-heading">
      <div className="flex shrink-0 items-start gap-3 border-b border-line pb-5">
        {onBack && (
          <Button
            variant="ghost"
            onClick={onBack}
            className="shrink-0 gap-1.5 rounded-md px-2 text-fog hover:bg-ink hover:text-paper"
          >
            <ArrowLeft className="size-4" aria-hidden="true" />
            {t.backToRuns}
          </Button>
        )}
        <div className="min-w-0">
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
        <div className="mt-3 flex min-h-0 flex-1 flex-col">
          <RunColumnHeader t={t} />
          <ol
            ref={listRef}
            className={`relative divide-y divide-line ${
              embedded ? `min-h-0 flex-1 overflow-y-auto ${SCROLL_CLASS}` : ""
            }`}
          >
            {board.calls.map((call) => {
              const forecast = predictions.get(call.station_stop_id)?.arr;
              return (
                <li
                  key={call.station_stop_id}
                  data-start={call === start ? "" : undefined}
                  className={RUN_ROW_HEIGHT_TITLED}
                >
                  <RunEntry
                    link={
                      onChoose
                        ? undefined
                        : v2Paths.train(call.train_id, stationId, call.service_date)
                    }
                    onSelect={
                      onChoose
                        ? () =>
                            onChoose(
                              {
                                key: `call-${call.station_stop_id}`,
                                trainId: call.train_id,
                                date: call.service_date,
                                stationId,
                              },
                              true,
                            )
                        : undefined
                    }
                    selected={chosen?.key === `call-${call.station_stop_id}` || isFromRun(call)}
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
