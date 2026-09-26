// The v2 search view, generated from v1's TrainSearch.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md).
import { ArrowRight, Clock3, MapPin, Route, Search, TrainFront } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useRouteDirections,
  useRouteStations,
  useRouteTripsAround,
  useSuggest,
} from "@/hooks/use-dbrel";
import { useModelPredictions } from "@/hooks/use-tabm";
import { delayColor, expectedTime } from "@/lib/delay";
import { copy, type Locale } from "@/lib/i18n";
import { trainRoute, type DelayPred, type RouteRef, type SuggestResult } from "@/lib/api";
import type { SidePrediction } from "@/lib/tabm";
import { ModelDelta } from "./ModelDelta";
import { v2Paths } from "./paths";

interface Props {
  locale: Locale;
}

function DelayBlock({
  scheduled,
  pred,
  modelPred,
  noAverageLabel,
}: {
  scheduled: string | null;
  pred: DelayPred | null;
  modelPred: SidePrediction | undefined;
  noAverageLabel: string;
}) {
  const expected = expectedTime(scheduled, pred);
  const hasModel = Boolean(scheduled) && modelPred !== undefined;
  if (expected && pred) {
    return (
      <>
        <span className={`block font-mono text-[10px] font-semibold ${delayColor(pred.p50_min)}`}>
          <span className="mr-1 font-normal text-fog">Ø</span>
          {expected} · +{pred.p50_min}
        </span>
        {hasModel && <ModelDelta pred={modelPred as SidePrediction} />}
      </>
    );
  }
  if (hasModel) {
    return <ModelDelta pred={modelPred as SidePrediction} />;
  }
  return <span className="block text-[10px] text-fog">{scheduled ? noAverageLabel : "—"}</span>;
}

function kindIcon(kind: SuggestResult["kind"]) {
  if (kind === "station") return <MapPin className="size-4 text-teal" />;
  if (kind === "line") return <Route className="size-4 text-teal" />;
  return <TrainFront className="size-4 text-teal" />;
}

interface SavedSearch {
  query: string;
  selectedLine: RouteRef | null;
  direction: number | undefined;
  stationId: number | undefined;
  date: string;
  time: string;
  submitted: boolean;
}

// Module-level, not browser storage: it survives navigating to a train page
// and back (the module stays loaded), and is empty again after a full reload
// -- which is also what the server renders, so hydration never mismatches.
let savedSearch: SavedSearch | null = null;

export function SearchV2({ locale }: Props) {
  const navigate = useNavigate();
  const t = copy[locale];

  const searchFieldRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState(savedSearch?.query ?? "");
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const [selectedLine, setSelectedLine] = useState<RouteRef | null>(
    savedSearch?.selectedLine ?? null,
  );
  const [direction, setDirection] = useState<number | undefined>(savedSearch?.direction);
  const [stationId, setStationId] = useState<number | undefined>(savedSearch?.stationId);
  const [date, setDate] = useState(savedSearch?.date ?? "");
  const [time, setTime] = useState(savedSearch?.time ?? "");
  // The prediction request only ever fires from the Prognose button, never
  // just because every field happens to be filled in -- and any further edit
  // invalidates a result that no longer matches what's on screen, so it's
  // withdrawn until the button is pressed again.
  const [submitted, setSubmitted] = useState(savedSearch?.submitted ?? false);

  useEffect(() => {
    savedSearch = { query, selectedLine, direction, stationId, date, time, submitted };
  }, [query, selectedLine, direction, stationId, date, time, submitted]);

  const { data: suggestData } = useSuggest(query);
  const matches = suggestData?.results ?? [];

  const route = selectedLine ?? undefined;
  const { data: directionsData } = useRouteDirections(route);
  const directions = directionsData?.directions ?? [];

  const { data: stationsData } = useRouteStations(route, direction as 0 | 1 | undefined);
  const stations = stationsData?.stations ?? [];

  const { data: aroundData, isLoading: aroundLoading } = useRouteTripsAround(
    route,
    direction as 0 | 1 | undefined,
    stationId,
    date,
    time,
    submitted,
  );
  const trips = aroundData?.trips ?? [];
  const tripKey = (trip: (typeof trips)[number]) =>
    `${trip.train_id}-${trip.sched_dep ?? trip.sched_arr}`;
  const predictions = useModelPredictions(
    aroundData?.model_stamp,
    trips.map((trip) => ({ key: tripKey(trip), model_input: trip.model_input })),
  );

  const canSubmit =
    selectedLine !== null &&
    direction !== undefined &&
    stationId !== undefined &&
    Boolean(date) &&
    Boolean(time);

  useEffect(() => {
    if (!suggestionsOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!searchFieldRef.current?.contains(event.target as Node)) {
        setSuggestionsOpen(false);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [suggestionsOpen]);

  // A line or a specific train both resolve to the SAME gated flow (pick
  // direction, stop, date, time, then press Prognose) -- picking a train by
  // name is just a shortcut to its line (or, for a train with no line, the
  // route grouped around it), never a way to skip the gate. Only a station
  // is its own, immediate destination (a live board, not a prediction for a
  // chosen stop/time).
  const chooseSuggestion = async (result: SuggestResult) => {
    setSuggestionsOpen(false);
    if (result.kind === "station") {
      void navigate(v2Paths.station(result.id));
      return;
    }

    setDirection(undefined);
    setStationId(undefined);
    setSubmitted(false);

    if (result.kind === "line") {
      setSelectedLine({ kind: "line", id: result.id });
      setQuery(result.name);
      return;
    }

    // result.kind === "train": resolve its line before anything else can be
    // picked -- there is no real "this train's own reverse direction" (the
    // mock's version of that was fabricated); the line's real two directions
    // are what "direction" means here. Long-distance trains (ICE, IC, ...)
    // have no line, so the backend's route grouped around the train is used.
    setQuery(result.name);
    setSelectedLine(null);
    const trainInfo = await trainRoute(result.id).catch(() => null);
    if (!trainInfo) return;
    setSelectedLine(
      trainInfo.train.line
        ? { kind: "line", id: trainInfo.train.line.id }
        : { kind: "corridor", id: result.id },
    );
  };

  return (
    <div>
      <Card className="rounded-lg border border-line bg-panel p-4 text-paper shadow-none sm:p-5">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <div className="relative" ref={searchFieldRef}>
            <label htmlFor="train-search" className="mb-1.5 block text-[11px] uppercase text-fog">
              {t.trainLabel}
            </label>
            <div className="flex h-[46px] items-center gap-2 rounded-md border border-line bg-ink px-3 focus-within:border-teal">
              <Search className="size-4 shrink-0 text-fog" aria-hidden="true" />
              <input
                id="train-search"
                value={query}
                onFocus={() => setSuggestionsOpen(Boolean(query.trim()))}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setSelectedLine(null);
                  setDirection(undefined);
                  setStationId(undefined);
                  setSubmitted(false);
                  setSuggestionsOpen(Boolean(event.target.value.trim()));
                }}
                placeholder={t.trainPlaceholder}
                autoComplete="off"
                aria-autocomplete="list"
                aria-controls="train-suggestions"
                aria-expanded={suggestionsOpen && matches.length > 0}
                className="min-w-0 flex-1 bg-transparent text-sm font-medium text-paper outline-none placeholder:text-fog/60"
              />
            </div>
            {suggestionsOpen && query.trim() && (
              <div
                id="train-suggestions"
                role="listbox"
                className="absolute z-20 mt-2 w-full min-w-[20rem] overflow-hidden rounded-md border border-line bg-panel shadow-lg"
              >
                {matches.length > 0 ? (
                  matches.map((result) => (
                    <Button
                      key={`${result.kind}-${result.id}`}
                      variant="ghost"
                      role="option"
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => chooseSuggestion(result)}
                      className="h-auto w-full justify-start rounded-none px-3 py-3 text-left hover:bg-ink"
                    >
                      <span className="flex min-w-0 items-start gap-2">
                        <span className="mt-0.5">{kindIcon(result.kind)}</span>
                        <span className="flex min-w-0 flex-col">
                          <span className="font-display font-semibold text-paper">
                            {result.name}
                          </span>
                          {result.stops && (
                            <span className="whitespace-normal text-[11px] font-normal text-fog">
                              {result.stops}
                            </span>
                          )}
                        </span>
                      </span>
                    </Button>
                  ))
                ) : (
                  <p className="px-3 py-4 text-sm text-fog">{t.noResults}</p>
                )}
              </div>
            )}
          </div>
          <div>
            <label
              htmlFor="train-direction"
              className="mb-1.5 block text-[11px] uppercase text-fog"
            >
              {t.direction}
            </label>
            <select
              id="train-direction"
              value={direction ?? ""}
              onChange={(event) => {
                setDirection(event.target.value === "" ? undefined : Number(event.target.value));
                setStationId(undefined);
                setSubmitted(false);
              }}
              disabled={!selectedLine || directions.length === 0}
              className="h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal disabled:opacity-50"
            >
              <option value="">{t.allDirections}</option>
              {directions.map((d) => (
                <option key={d.direction} value={d.direction}>
                  {d.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="train-stop" className="mb-1.5 block text-[11px] uppercase text-fog">
              {t.selectedStop}
            </label>
            <select
              id="train-stop"
              value={stationId ?? ""}
              onChange={(event) => {
                setStationId(event.target.value === "" ? undefined : Number(event.target.value));
                setSubmitted(false);
              }}
              disabled={stations.length === 0}
              className="h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal disabled:opacity-50"
            >
              <option value="">—</option>
              {stations.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="train-date" className="mb-1.5 block text-[11px] uppercase text-fog">
              {t.searchDate}
            </label>
            <input
              id="train-date"
              type="date"
              value={date}
              onChange={(event) => {
                setDate(event.target.value);
                setSubmitted(false);
              }}
              disabled={!selectedLine}
              className="h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal disabled:opacity-50"
            />
          </div>
          <div>
            <label htmlFor="train-time" className="mb-1.5 block text-[11px] uppercase text-fog">
              {t.searchTime}
            </label>
            <input
              id="train-time"
              type="time"
              value={time}
              onChange={(event) => {
                setTime(event.target.value);
                setSubmitted(false);
              }}
              disabled={!selectedLine}
              className="h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal disabled:opacity-50"
            />
          </div>
        </div>
        <Button
          onClick={() => setSubmitted(true)}
          disabled={!canSubmit}
          className="mt-3 h-[42px] w-full rounded-md bg-teal font-display font-semibold text-action-foreground hover:bg-teal disabled:opacity-40 sm:w-auto sm:px-8"
        >
          {t.getPrognose}
        </Button>
      </Card>

      {!submitted && selectedLine && (
        <Alert className="mx-auto mt-4 max-w-3xl border-line bg-panel">
          <AlertDescription className="text-center text-xs text-fog">
            {t.pickAllFields}
          </AlertDescription>
        </Alert>
      )}

      {submitted && (
        <Card
          className="mx-auto mt-6 max-w-3xl rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
          aria-live="polite"
        >
          <div className="flex items-center gap-3 border-b border-line pb-4">
            <span className="grid size-9 place-items-center rounded-md bg-ink text-teal">
              <MapPin className="size-4" />
            </span>
            <div>
              <p className="text-[11px] uppercase text-fog">{t.serviceAt}</p>
              <h2 className="mt-0.5 font-display text-lg font-semibold text-paper">
                {stations.find((s) => s.id === stationId)?.name ?? t.selectedStop}
              </h2>
            </div>
          </div>
          <div className="divide-y divide-line">
            {aroundLoading ? (
              <div className="space-y-3 px-1 py-4" aria-label={t.loading}>
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full bg-line" />
                ))}
              </div>
            ) : trips.length > 0 ? (
              trips.map((trip) => {
                const label = trip.relation === "after" ? t.selectedService : t.previousService;
                const modelPred = predictions.get(tripKey(trip));
                return (
                  <Button
                    key={tripKey(trip)}
                    variant="ghost"
                    onClick={() => void navigate(v2Paths.train(trip.train_id))}
                    className="h-auto w-full items-start justify-between gap-4 rounded-none px-1 py-4 text-left hover:bg-ink sm:px-3"
                  >
                    <span className="flex min-w-0 items-start gap-3">
                      <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-teal text-action-foreground">
                        <TrainFront className="size-4" />
                      </span>
                      <span className="min-w-0">
                        <span className="flex items-center gap-1 text-[10px] uppercase text-fog">
                          {label}
                        </span>
                        <span className="mt-1 block font-display font-semibold text-paper">
                          {trip.name}
                        </span>
                      </span>
                    </span>
                    <span className="flex shrink-0 flex-col items-end gap-1 text-right">
                      <span>
                        <span className="block text-[10px] uppercase font-normal text-fog">
                          {t.arrival}
                        </span>
                        <span className="mt-1 block font-mono text-sm text-mist">
                          {trip.sched_arr ?? "—"}
                        </span>
                        <DelayBlock
                          scheduled={trip.sched_arr}
                          pred={trip.arr_pred}
                          modelPred={modelPred?.arr}
                          noAverageLabel={t.noAverage}
                        />
                      </span>
                      <span className="mt-2 flex items-center justify-end gap-1 text-[10px] font-normal text-fog">
                        <Clock3 className="size-3" />
                        {t.openRoute}
                        <ArrowRight className="size-3" />
                      </span>
                    </span>
                  </Button>
                );
              })
            ) : (
              <p className="px-1 py-8 text-center text-sm text-fog">{t.nothingScheduled}</p>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
