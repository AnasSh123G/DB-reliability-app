// The v2 search view, generated from v1's TrainSearch.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md).
import {
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Clock3,
  MapPin,
  Route,
  Search,
  TrainFront,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
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
import { copy, type Copy, type Locale } from "@/lib/i18n";
import { trainRoute, type RouteRef, type SuggestResult, type TripAround } from "@/lib/api";
import type { SidePrediction } from "@/lib/tabm";
import { ModelDelta } from "./ModelDelta";
import { TrainNumber } from "./TrainNumber";
import { v2Paths } from "./paths";

interface Props {
  locale: Locale;
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
  return (
    <span className="block text-[12.67px] text-fog">{scheduled ? noPredictionLabel : "—"}</span>
  );
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
  shift: number;
}

// How many runs the Prognose card shows at once, and how long its slide animation takes.
const WINDOW_SIZE = 3;
const SLIDE_MS = 300;

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
  // How far the window of runs is slid from the chosen time: -1 is one run earlier, +1 one later.
  const [shift, setShift] = useState(savedSearch?.shift ?? 0);

  useEffect(() => {
    savedSearch = { query, selectedLine, direction, stationId, date, time, submitted, shift };
  }, [query, selectedLine, direction, stationId, date, time, submitted, shift]);

  // Editing any field withdraws the result, and the next Prognose starts at the chosen time again.
  useEffect(() => {
    if (!submitted) setShift(0);
  }, [submitted]);

  const { data: suggestData } = useSuggest(query);
  const matches = suggestData?.results ?? [];

  const route = selectedLine ?? undefined;
  const { data: directionsData } = useRouteDirections(route);
  const directions = directionsData?.directions ?? [];

  const { data: stationsData } = useRouteStations(route, direction as 0 | 1 | undefined);
  const stations = stationsData?.stations ?? [];

  const {
    data: aroundData,
    isLoading: aroundLoading,
    isPlaceholderData: aroundSwitching,
  } = useRouteTripsAround(
    route,
    direction as 0 | 1 | undefined,
    stationId,
    date,
    time,
    submitted,
    shift,
  );
  const trips = aroundData?.trips ?? [];
  const tripKey = (trip: (typeof trips)[number]) =>
    `${trip.train_id}-${trip.sched_dep ?? trip.sched_arr}`;
  const predictions = useModelPredictions(
    aroundData?.model_stamp,
    trips.map((trip) => ({ key: tripKey(trip), model_input: trip.model_input })),
  );

  // The server returns every run the slid window needs; the 3 shown are a slice of them. The
  // slice follows the shift the data was fetched for, not the button state, so it never mixes
  // one answer with another's position while the next one loads.
  const dataShift = aroundData?.shift ?? 0;
  const visible = useMemo(
    () => trips.slice(Math.max(0, dataShift), Math.max(0, dataShift) + WINDOW_SIZE),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [aroundData],
  );
  // The window can slide while it would not repeat a run: n_calls distinct runs exist at this stop.
  const nCalls = aroundData?.n_calls ?? 0;
  const canSlide = (next: number) => WINDOW_SIZE + Math.abs(next) <= nCalls;
  const canEarlier = !aroundSwitching && canSlide(shift - 1);
  const canLater = !aroundSwitching && canSlide(shift + 1);

  // What changed since the last answer shown: the runs that came in and the run that went out,
  // for the slide animation. Worked out while rendering, so the first frame already has them.
  const shown = useRef<{ shift: number; trips: TripAround[] } | null>(null);
  const motion = useMemo(() => {
    const before = shown.current;
    if (!before || before.shift === dataShift) return null;
    const nowKeys = new Set(visible.map(tripKey));
    const beforeKeys = new Set(before.trips.map(tripKey));
    return {
      toEarlier: dataShift < before.shift,
      entering: new Set(visible.map(tripKey).filter((key) => !beforeKeys.has(key))),
      leaving: before.trips.filter((trip) => !nowKeys.has(tripKey(trip))),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aroundData]);
  const [settledFor, setSettledFor] = useState<unknown>(null);
  useEffect(() => {
    shown.current = submitted ? { shift: dataShift, trips: visible } : null;
    if (!motion) return;
    const timer = setTimeout(() => setSettledFor(aroundData), SLIDE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aroundData, submitted]);
  const moving = motion !== null && settledFor !== aroundData ? motion : null;
  // Rows in on-screen order: the run that is leaving stays at the end it slides out of.
  const rows = [
    ...(moving && !moving.toEarlier ? moving.leaving.map((trip) => ({ trip, leaving: true })) : []),
    ...visible.map((trip) => ({ trip, leaving: false })),
    ...(moving?.toEarlier ? moving.leaving.map((trip) => ({ trip, leaving: true })) : []),
  ];

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
            <label
              htmlFor="train-search"
              className="mb-1.5 block text-[13.67px] uppercase text-fog"
            >
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
                            <TrainNumber name={result.name} numberName={result.number_name} />
                          </span>
                          {result.stops && (
                            <span className="whitespace-normal text-[13.67px] font-normal text-fog">
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
              className="mb-1.5 block text-[13.67px] uppercase text-fog"
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
            <label htmlFor="train-stop" className="mb-1.5 block text-[13.67px] uppercase text-fog">
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
            <label htmlFor="train-date" className="mb-1.5 block text-[13.67px] uppercase text-fog">
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
            <label htmlFor="train-time" className="mb-1.5 block text-[13.67px] uppercase text-fog">
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
              <p className="text-[13.67px] uppercase text-fog">{t.serviceAt}</p>
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
            ) : rows.length > 0 ? (
              rows.map(({ trip, leaving }) => {
                const slide = moving?.toEarlier ? "top" : "bottom";
                const motionClass = leaving
                  ? `pointer-events-none animate-out fade-out-0 duration-300 ${
                      moving?.toEarlier ? "slide-out-to-bottom-6" : "slide-out-to-top-6"
                    }`
                  : moving?.entering.has(tripKey(trip))
                    ? `animate-in fade-in-0 duration-300 slide-in-from-${slide}-6`
                    : "";
                const label = trip.relation === "after" ? t.selectedService : t.previousService;
                const modelPred = predictions.get(tripKey(trip));
                // A run that starts at this stop has no arrival to predict: show its departure.
                const startsHere = trip.sched_arr === null && trip.sched_dep !== null;
                return (
                  <div key={tripKey(trip)} className={motionClass}>
                    <Button
                      variant="ghost"
                      onClick={() => void navigate(v2Paths.train(trip.train_id, stationId))}
                      className="h-auto w-full items-start justify-between gap-4 rounded-none px-1 py-4 text-left hover:bg-ink sm:px-3"
                    >
                      <span className="flex min-w-0 items-start gap-3">
                        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-teal text-action-foreground">
                          <TrainFront className="size-4" />
                        </span>
                        <span className="min-w-0">
                          <span className="flex items-center gap-1 text-[12.67px] uppercase text-fog">
                            {label}
                          </span>
                          <span className="mt-1 block font-display font-semibold text-paper">
                            {trip.name}
                            <TrainNumber name={trip.name} numberName={trip.number_name} />
                          </span>
                          {startsHere && (
                            <Badge
                              variant="outline"
                              className="mt-1 border-amber text-[12.67px] font-normal text-amber"
                            >
                              {t.startsHere}
                            </Badge>
                          )}
                        </span>
                      </span>
                      <span className="flex shrink-0 flex-col items-end gap-1 text-right">
                        <span>
                          <span className="block text-[12.67px] uppercase font-normal text-fog">
                            {startsHere ? t.departure : t.arrival}
                          </span>
                          <span className="mt-1 block font-mono text-sm text-mist">
                            {(startsHere ? trip.sched_dep : trip.sched_arr) ?? "—"}
                          </span>
                          {!startsHere && (
                            <DelayBlock
                              scheduled={trip.sched_arr}
                              modelPred={modelPred?.arr}
                              noPredictionLabel={t.noPrediction}
                              t={t}
                            />
                          )}
                        </span>
                        <span className="mt-2 flex items-center justify-end gap-1 text-[12.67px] font-normal text-fog">
                          <Clock3 className="size-3" />
                          {t.openRoute}
                          <ArrowRight className="size-3" />
                        </span>
                      </span>
                    </Button>
                  </div>
                );
              })
            ) : (
              <p className="px-1 py-8 text-center text-sm text-fog">{t.nothingScheduled}</p>
            )}
          </div>
          {rows.length > 0 && (
            <div className="flex gap-3 border-t border-line px-1 pt-4 sm:px-3">
              <Button
                variant="outline"
                onClick={() => setShift(shift - 1)}
                disabled={!canEarlier}
                className="h-[42px] flex-1 gap-2 border-line bg-ink text-paper hover:bg-panel disabled:opacity-40"
              >
                <ChevronUp className="size-4" />
                {t.showEarlier}
              </Button>
              <Button
                variant="outline"
                onClick={() => setShift(shift + 1)}
                disabled={!canLater}
                className="h-[42px] flex-1 gap-2 border-line bg-ink text-paper hover:bg-panel disabled:opacity-40"
              >
                <ChevronDown className="size-4" />
                {t.showLater}
              </Button>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
