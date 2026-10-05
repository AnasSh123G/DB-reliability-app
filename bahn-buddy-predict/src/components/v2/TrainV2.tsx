// The v2 train route view, generated from v1's TrainOverview.tsx so it renders and behaves like
// it, with its own visual identity (see
// openspec/changes/redesign-v2-visual-identity/design.md). The desktop route list uses `Table`;
// phone width keeps a stacked list, since `Table` doesn't reflow.
import { ArrowLeft, MapPin } from "lucide-react";
import type { ReactNode } from "react";
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
import { useTrainRoute } from "@/hooks/use-dbrel";
import { useModelPredictions } from "@/hooks/use-tabm";
import { copy, type Copy, type Locale } from "@/lib/i18n";
import type { SidePrediction } from "@/lib/tabm";
import { ModelDelta } from "./ModelDelta";
import { TrainNumber } from "./TrainNumber";
import { v2Paths } from "./paths";

interface Props {
  trainId: number;
  locale: Locale;
  bare?: boolean;
  className?: string;
}

function DelayCell({
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
  return <span className="block text-mist">{scheduled ? noPredictionLabel : "—"}</span>;
}

export function TrainV2({ trainId, locale, bare = false, className }: Props) {
  const t = copy[locale];
  const { data: route, isLoading, isError } = useTrainRoute(trainId);
  const predictions = useModelPredictions(
    route?.model_stamp,
    (route?.stops ?? []).map((stop) => ({ key: stop.seq, model_input: stop.model_input })),
  );

  const wrapperClass = `${bare ? "" : "rise mt-6 rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"} ${className ?? ""}`;

  if (isLoading) {
    return (
      <Card className={wrapperClass} aria-label={t.loading}>
        <Skeleton className="h-7 w-2/3 bg-line" />
        <Skeleton className="mt-2 h-4 w-1/3 bg-line" />
        <div className="mt-5 space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full bg-line" />
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

  const { train, stops, n_monitored } = route;

  const nameNodeFor = (stop: (typeof stops)[number]): ReactNode =>
    stop.monitored && stop.station_id ? (
      <Link {...v2Paths.station(stop.station_id)} className="hover:underline">
        {stop.name}
      </Link>
    ) : (
      stop.name
    );

  return (
    <Card className={wrapperClass} role="region" aria-labelledby="train-heading">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-5">
        <div className="flex items-start gap-3">
          {!bare && (
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
          <div>
            <p className="text-xs text-fog">
              {t.trainRoute} {train.destination ?? "—"}
            </p>
            <h2 id="train-heading" className="mt-1 font-display text-2xl font-semibold text-paper">
              {train.name}
              <TrainNumber name={train.name} numberName={train.number_name} />
              {train.replacement_bus && (
                <Badge
                  variant="outline"
                  className="ml-2 align-middle border-amber font-mono text-[10px] font-normal text-amber"
                >
                  {t.replacementBus}
                </Badge>
              )}
            </h2>
            <p className="mt-1 text-xs text-fog">
              {[train.category, train.operator].filter(Boolean).join(" · ")}
              {" · "}
              {train.n_stops} {t.stops}
            </p>
            {train.line && (
              <Link
                {...v2Paths.line(train.line.id)}
                className="mt-1 inline-block text-xs text-teal hover:underline"
              >
                {train.line.name}
              </Link>
            )}
          </div>
        </div>
      </div>

      {/* Phone: a stacked list -- `Table` doesn't reflow at narrow widths. */}
      <ol className="mt-2 sm:hidden">
        {stops.map((stop) => (
          <li key={stop.seq} className="border-t border-line py-4">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid size-6 shrink-0 place-items-center rounded-lg bg-ink text-teal">
                <MapPin className="size-3.5" />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-paper">{nameNodeFor(stop)}</p>
                {!stop.monitored && (
                  <Badge variant="outline" className="mt-1 text-[10px] font-normal text-fog">
                    {t.notMonitored}
                  </Badge>
                )}
              </div>
            </div>

            {stop.monitored && (
              <div className="mt-4 grid grid-cols-2 gap-x-5 gap-y-4">
                <div>
                  <p className="text-[10px] uppercase text-fog">{t.arrival}</p>
                  <p className="mt-1 font-mono text-sm text-mist">{stop.arr ?? "—"}</p>
                </div>
                <div>
                  <p className="text-[10px] uppercase text-fog">{t.expectedArrival}</p>
                  <p className="mt-1 text-sm">
                    <DelayCell
                      scheduled={stop.arr}
                      modelPred={predictions.get(stop.seq)?.arr}
                      noPredictionLabel={t.noPrediction}
                      t={t}
                    />
                  </p>
                </div>
              </div>
            )}
          </li>
        ))}
      </ol>

      {/* Desktop: the same data as a real table. */}
      <div className="mt-5 hidden sm:block">
        <Table>
          <TableHeader>
            <TableRow className="border-line hover:bg-transparent">
              <TableHead className="text-[10px] uppercase text-fog">{t.stop}</TableHead>
              <TableHead className="text-[10px] uppercase text-fog">{t.arrival}</TableHead>
              <TableHead className="text-[10px] uppercase text-fog">{t.expectedArrival}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {stops.map((stop) => (
              <TableRow key={stop.seq} className="border-line hover:bg-ink/40">
                <TableCell className="min-w-0">
                  <div className="flex min-w-0 items-center gap-3">
                    <span
                      className={`grid size-6 shrink-0 place-items-center rounded-lg bg-ink ${stop.monitored ? "text-teal" : "text-fog"}`}
                    >
                      <MapPin className="size-3.5" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-paper">{nameNodeFor(stop)}</p>
                    </div>
                  </div>
                </TableCell>
                {stop.monitored ? (
                  <>
                    <TableCell className="font-mono text-sm text-mist">{stop.arr ?? "—"}</TableCell>
                    <TableCell className="text-sm">
                      <DelayCell
                        scheduled={stop.arr}
                        modelPred={predictions.get(stop.seq)?.arr}
                        noPredictionLabel={t.noPrediction}
                        t={t}
                      />
                    </TableCell>
                  </>
                ) : (
                  <TableCell colSpan={2}>
                    <Badge variant="outline" className="text-[11px] font-normal text-fog">
                      {t.notMonitored}
                    </Badge>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {n_monitored === 0 && (
        <Alert className="mt-4 border-line bg-ink">
          <AlertDescription className="text-xs text-fog">{t.noMonitoredStops}</AlertDescription>
        </Alert>
      )}
    </Card>
  );
}
