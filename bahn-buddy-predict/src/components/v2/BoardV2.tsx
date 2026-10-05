// The v2 station board, generated from v1's StationBoard.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md).
// The desktop board uses `Table`; phone width keeps a stacked list, since `Table` doesn't
// reflow.
import { ArrowLeft, MapPin } from "lucide-react";
import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useStationBoard } from "@/hooks/use-dbrel";
import { useModelPredictions } from "@/hooks/use-tabm";
import type { StationBoardCall } from "@/lib/api";
import { copy, type Copy, type Locale } from "@/lib/i18n";
import type { SidePrediction } from "@/lib/tabm";
import { ModelDelta } from "./ModelDelta";
import { TrainNumber } from "./TrainNumber";
import { v2Paths } from "./paths";

interface Props {
  stationId: number;
  locale: Locale;
}

function CallTags({ call, endsHereLabel }: { call: StationBoardCall; endsHereLabel: string }) {
  return (
    <p className="mt-0.5 flex flex-wrap items-center gap-1.5 truncate text-[11px] text-fog">
      <span className="truncate">{call.headline}</span>
      {call.terminates && (
        <Badge variant="outline" className="border-amber text-[10px] font-normal text-amber">
          {endsHereLabel}
        </Badge>
      )}
    </p>
  );
}

function DelayBlock({
  scheduled,
  modelPred,
  noPredictionLabel,
  t,
}: {
  scheduled: string | null;
  modelPred: SidePrediction | undefined;
  noPredictionLabel: string;
  t: Copy;
}) {
  if (scheduled && modelPred !== undefined) {
    return <ModelDelta pred={modelPred} scheduled={scheduled} t={t} />;
  }
  return <span className="block text-[10px] text-fog">{scheduled ? noPredictionLabel : "—"}</span>;
}

export function BoardV2({ stationId, locale }: Props) {
  const t = copy[locale];
  const [widened, setWidened] = useState(false);
  const {
    data: board,
    isLoading,
    isError,
  } = useStationBoard(stationId, widened ? { backMin: 120, fwdMin: 240 } : undefined);
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
            {t.windowLabel}: {board.window.from} – {board.window.to} ({board.window.date})
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
        <>
          {/* Phone: a stacked list -- `Table` doesn't reflow at narrow widths. */}
          <ol className="mt-2 sm:hidden">
            {board.calls.map((call) => {
              return (
                <li
                  key={call.station_stop_id}
                  className="flex flex-wrap items-center justify-between gap-3 border-t border-line py-4"
                >
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                      <MapPin className="size-4" />
                    </span>
                    <div className="min-w-0">
                      <Link
                        {...v2Paths.train(call.train_id)}
                        className="font-display font-semibold text-paper hover:underline"
                      >
                        {call.train}
                      </Link>
                      <TrainNumber name={call.train} numberName={call.number_name} />
                      <CallTags call={call} endsHereLabel={t.endsHere} />
                    </div>
                  </div>
                  <div className="shrink-0 text-right">
                    <span className="block text-[10px] uppercase font-normal text-fog">
                      {t.arrival}
                    </span>
                    <span className="mt-1 block font-mono text-sm text-mist">
                      {call.arr ?? "—"}
                    </span>
                    <DelayBlock
                      scheduled={call.arr}
                      modelPred={predictions.get(call.station_stop_id)?.arr}
                      noPredictionLabel={t.noPrediction}
                      t={t}
                    />
                  </div>
                </li>
              );
            })}
          </ol>

          {/* Desktop: the same data as a real table. */}
          <div className="mt-2 hidden sm:block">
            <Table>
              <TableHeader>
                <TableRow className="border-line hover:bg-transparent">
                  <TableHead />
                  <TableHead className="text-[10px] uppercase text-fog">{t.arrival}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {board.calls.map((call) => {
                  return (
                    <TableRow key={call.station_stop_id} className="border-line hover:bg-ink/40">
                      <TableCell className="min-w-0">
                        <div className="flex min-w-0 items-center gap-3">
                          <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                            <MapPin className="size-4" />
                          </span>
                          <div className="min-w-0">
                            <Link
                              {...v2Paths.train(call.train_id)}
                              className="font-display font-semibold text-paper hover:underline"
                            >
                              {call.train}
                            </Link>
                            <TrainNumber name={call.train} numberName={call.number_name} />
                            <CallTags call={call} endsHereLabel={t.endsHere} />
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <span className="block font-mono text-sm text-mist">{call.arr ?? "—"}</span>
                        <DelayBlock
                          scheduled={call.arr}
                          modelPred={predictions.get(call.station_stop_id)?.arr}
                          noPredictionLabel={t.noPrediction}
                          t={t}
                        />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </>
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
