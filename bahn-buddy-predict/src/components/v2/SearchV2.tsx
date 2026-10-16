// The v2 search view, generated from v1's TrainSearch.tsx so it renders and behaves like it,
// with its own visual identity (see openspec/changes/redesign-v2-visual-identity/design.md) and
// the differences listed in openspec/changes/improve-v2-search-and-prognose.
import { ChevronDown, ChevronUp, MapPin, Route, TrainFront } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useRouteDirections, useRouteStations, useSuggest } from "@/hooks/use-dbrel";
import { useRunWindow, WINDOW_SIZE, type RunSlot } from "@/hooks/use-run-window";
import { copy, type Locale } from "@/lib/i18n";
import { trainRoute, type RouteRef, type SuggestResult } from "@/lib/api";
import { isStateCode, statesFor, type StateCode } from "@/lib/states";
import { FieldLabel } from "./FieldLabel";
import { RUN_ROW_HEIGHT, RunColumnHeader, RunEntry } from "./RunEntry";
import { TrainNumber } from "./TrainNumber";
import { BoardV2 } from "./BoardV2";
import { icons } from "./icons";
import { addDays, formatRunDay, type RunChoice } from "./run-format";
import { TrainV2 } from "./TrainV2";

interface Props {
  locale: Locale;
  /** A station whose board to show under the form (from the address), for a date and time or now. */
  initialStation?: { id: number; date?: string | undefined; time?: string | undefined } | undefined;
}

function kindIcon(kind: SuggestResult["kind"]) {
  if (kind === "station") return <MapPin className="size-4 text-teal" />;
  if (kind === "line") return <Route className="size-4 text-teal" />;
  return <TrainFront className="size-4 text-teal" />;
}

/** The board shown under the form: a station, for a date and time or (without) for now. */
interface BoardFor {
  id: number;
  date?: string | undefined;
  time?: string | undefined;
  /** For a stop of a route: the run the user came from, marked on the board. */
  fromRun?: { trainId: number; date: string } | undefined;
}

interface SavedSearch {
  query: string;
  selectedLine: RouteRef | null;
  selectedStation: { id: number; name: string } | null;
  boardFor: BoardFor | null;
  selectedLabel: string;
  direction: number | undefined;
  stationId: number | undefined;
  date: string;
  time: string;
  submitted: boolean;
  start: number;
  chosen: RunChoice | null;
  stopBoard: BoardFor | null;
}

// How long the Prognose card's slide animation takes.
const SLIDE_MS = 400;

// The height of a station board under the form, and of a stop's board; what does not fit scrolls
// inside the card.
const RESULT_HEIGHT = "h-[36rem] lg:h-[40rem]";
// Beside the list the route takes the list's height (the Prognose card's, or a board's); under it,
// on a phone or tablet, a fixed one. Its stops scroll inside it.
const ROUTE_HEIGHT = "h-[36rem] lg:absolute lg:inset-0 lg:h-auto";

// Module-level, not browser storage: it survives navigating to a train page
// and back (the module stays loaded), and is empty again after a full reload
// -- which is also what the server renders, so hydration never mismatches.
let savedSearch: SavedSearch | null = null;

// The chosen Bundesland is a per-browser convenience, kept across visits.
const STATE_KEY = "bahnpredict:state";

/**
 * The Bundesland this browser chose last time, if any.
 *
 * @returns its code, or "" for all of Germany (also when storage is unavailable).
 */
function readSavedState(): StateCode | "" {
  try {
    const stored = localStorage.getItem(STATE_KEY) ?? "";
    return isStateCode(stored) ? stored : "";
  } catch {
    return "";
  }
}

/**
 * A slot's identity on the page: its position, not its run. A short day wraps around, so one run
 * can sit at two positions; the slide must move by positions, not by which run is where.
 */
const slotKey = (slot: RunSlot) => `position-${slot.position}`;

/** Today's date in the browser's time zone, YYYY-MM-DD. */
const todayDate = () => {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
};

/** The time now in the browser's time zone, HH:MM. */
const nowTime = () => {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
};

const QUICK_CLASS =
  "inline-flex h-[42px] w-full items-center justify-center rounded-md border border-line bg-ink px-4 font-display text-[15px] font-semibold text-teal hover:bg-panel disabled:cursor-not-allowed disabled:opacity-40";

const FIELD_CLASS =
  "h-[46px] w-full rounded-md border border-line bg-ink px-3 text-sm font-medium text-paper outline-none focus:border-teal disabled:cursor-not-allowed disabled:opacity-50";

export function SearchV2({ locale, initialStation }: Props) {
  const navigate = useNavigate();
  const t = copy[locale];

  const searchFieldRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState(savedSearch?.query ?? "");
  // Read after the first render, not in useState's initializer: the server renders "" too, so
  // hydration never mismatches.
  const [region, setRegion] = useState<StateCode | "">("");
  useEffect(() => setRegion(readSavedState()), []);
  // Changing the state clears what was picked in the old one: the search text, the chosen line,
  // train or station, its direction and stop, and any result on screen (date and time stay).
  const chooseRegion = (next: StateCode | "") => {
    if (next !== region) {
      setQuery("");
      setSelectedLine(null);
      setSelectedStation(null);
      setSelectedLabel("");
      setDirection(undefined);
      setStationId(undefined);
      withdraw();
      setSuggestionsOpen(false);
    }
    setRegion(next);
    try {
      localStorage.setItem(STATE_KEY, next);
    } catch {
      // per-viewer convenience only; fine if storage is unavailable
    }
  };
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const [selectedLine, setSelectedLine] = useState<RouteRef | null>(
    savedSearch?.selectedLine ?? null,
  );
  // A chosen station: direction and stop do not apply, only a date and time for its board.
  const [selectedStation, setSelectedStation] = useState(savedSearch?.selectedStation ?? null);
  // The name the user picked in the suggestions; the Prognose card shows it once, in its header.
  const [selectedLabel, setSelectedLabel] = useState(savedSearch?.selectedLabel ?? "");
  const [direction, setDirection] = useState<number | undefined>(savedSearch?.direction);
  const [stationId, setStationId] = useState<number | undefined>(savedSearch?.stationId);
  const [date, setDate] = useState(savedSearch?.date ?? "");
  const [time, setTime] = useState(savedSearch?.time ?? "");
  // The prediction request only ever fires from the Prognose button, never
  // just because every field happens to be filled in -- and any further edit
  // invalidates a result that no longer matches what's on screen, so it's
  // withdrawn until the button is pressed again.
  const [submitted, setSubmitted] = useState(savedSearch?.submitted ?? false);
  // The board of a station, shown under the form like a Prognose result.
  const [boardFor, setBoardFor] = useState<BoardFor | null>(savedSearch?.boardFor ?? null);
  // The run whose route is shown beside the list (below it on a phone).
  const [chosen, setChosen] = useState<RunChoice | null>(savedSearch?.chosen ?? null);
  // A stop chosen on the route: its station's board replaces the list and the route, until Back.
  const [stopBoard, setStopBoard] = useState<BoardFor | null>(savedSearch?.stopBoard ?? null);
  const resultsRef = useRef<HTMLDivElement>(null);
  const showStopBoard = (next: BoardFor | null) => {
    setStopBoard(next);
    // The board takes the place of both cards: keep their top in view.
    requestAnimationFrame(() => {
      const top = resultsRef.current?.getBoundingClientRect().top;
      if (top !== undefined && top < 0) resultsRef.current?.scrollIntoView({ block: "start" });
    });
  };
  const routeRef = useRef<HTMLDivElement>(null);
  const choose = (choice: RunChoice, byUser: boolean) => {
    setChosen(choice);
    // On a phone the route is under the list: bring it into view when the user picked a run.
    if (byUser && !window.matchMedia("(min-width: 1024px)").matches)
      requestAnimationFrame(() =>
        routeRef.current?.scrollIntoView({ block: "start", behavior: "smooth" }),
      );
  };
  // Any edit withdraws what is shown under the form.
  const withdraw = () => {
    setSubmitted(false);
    setBoardFor(null);
    setChosen(null);
    setStopBoard(null);
  };

  const { data: suggestData } = useSuggest(query, region || undefined);
  const matches = suggestData?.results ?? [];

  const route = selectedLine ?? undefined;
  const { data: directionsData } = useRouteDirections(route);
  const directions = directionsData?.directions ?? [];

  const { data: stationsData } = useRouteStations(route, direction as 0 | 1 | undefined);
  const stations = stationsData?.stations ?? [];

  const { slots, start, canEarlier, canLater, step, predictions, isLoading, isError } =
    useRunWindow(
      route,
      direction as 0 | 1 | undefined,
      stationId,
      date,
      time,
      submitted,
      savedSearch?.start,
    );

  useEffect(() => {
    savedSearch = {
      query,
      selectedLine,
      selectedStation,
      boardFor,
      selectedLabel,
      direction,
      stationId,
      date,
      time,
      submitted,
      start,
      chosen,
      stopBoard,
    };
  }, [
    chosen,
    stopBoard,
    query,
    selectedLine,
    selectedStation,
    boardFor,
    selectedLabel,
    direction,
    stationId,
    date,
    time,
    submitted,
    start,
  ]);

  // What changed since the last answer shown: which way the window moved and which runs went out,
  // for the slide animation. Worked out while rendering, so the first frame already has them.
  const shown = useRef<{ start: number; slots: RunSlot[] } | null>(null);
  const motion = useMemo(() => {
    const before = shown.current;
    if (!before || before.start === start) return null;
    const nowKeys = new Set(slots.map(slotKey));
    return {
      toEarlier: start < before.start,
      leaving: before.slots.filter((slot) => slot.trip && !nowKeys.has(slotKey(slot))),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start]);
  const [settled, setSettled] = useState<unknown>(null);
  useEffect(() => {
    shown.current = submitted ? { start, slots } : null;
  });
  useEffect(() => {
    if (!motion) return;
    const timer = setTimeout(() => setSettled(motion), SLIDE_MS);
    return () => clearTimeout(timer);
  }, [motion]);
  const moving = motion !== null && settled !== motion ? motion : null;
  const stepping = moving !== null;

  // The animation is a cassette: every run moves together, behind a window that shows exactly
  // the runs in view. The window clips, so a run leaves by sliding out of sight; the runs that
  // left are kept on the page, just outside the window, until the slide is over. The strip is
  // drawn already moved by the height of the runs that changed, and slides back to rest.
  const cassetteRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const ghostsRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!moving || !cassetteRef.current || !listRef.current) return;
    let distance = 0;
    if (moving.toEarlier) {
      // The new runs come in at the top: move by their height.
      const rows = listRef.current.querySelectorAll<HTMLElement>("[data-trip]");
      const next = rows[moving.leaving.length];
      distance = next ? next.offsetTop - (rows[0]?.offsetTop ?? 0) : listRef.current.offsetHeight;
    } else {
      distance = ghostsRef.current?.offsetHeight ?? 0;
    }
    const from = moving.toEarlier ? -distance : distance;
    cassetteRef.current.animate(
      [{ transform: `translateY(${from}px)` }, { transform: "translateY(0)" }],
      { duration: SLIDE_MS, easing: "cubic-bezier(0.4, 0, 0.2, 1)" },
    );
  }, [moving]);

  const runChoice = (slot: RunSlot): RunChoice => ({
    key: `run-${slot.position}`,
    trainId: slot.trip!.train_id,
    date: addDays(date, slot.trip!.day_offset),
    stationId,
  });

  // One run's row: two lines in three columns (scheduled · delay · expected), always the same
  // height whatever the run is -- the date and any note have fixed places on the second line.
  // `ghost` is a run that just left the window: it is placed just outside the end it left from,
  // so it slides out of view with the strip.
  const runRow = (slot: RunSlot, ghost: boolean) => {
    const { trip } = slot;
    if (!trip) {
      return (
        <div
          key={slotKey(slot)}
          data-trip={slotKey(slot)}
          className={`${RUN_ROW_HEIGHT} px-2 py-2 sm:px-3`}
        >
          <Skeleton className="h-full w-full bg-line" aria-label={t.loading} />
        </div>
      );
    }
    const forecast = predictions.get(trip.position)?.arr;
    const pending = forecast === undefined && trip.model_input !== null;
    // A line's runs are all the searched line; only a route of differently numbered trains (an ICE
    // route) names each run, and only when it is not the searched train itself.
    const ownName = selectedLine?.kind === "corridor" && trip.name !== selectedLabel;
    return (
      <div
        key={slotKey(slot)}
        data-trip={ghost ? undefined : slotKey(slot)}
        aria-hidden={ghost || undefined}
        className={`${RUN_ROW_HEIGHT} ${
          ghost
            ? `pointer-events-none border-line ${moving?.toEarlier ? "border-t" : "border-b"}`
            : ""
        }`}
      >
        <RunEntry
          onSelect={ghost ? undefined : () => choose(runChoice(slot), true)}
          selected={!ghost && chosen?.key === runChoice(slot).key}
          arrival={trip.sched_arr}
          departure={trip.sched_dep}
          forecast={forecast}
          pending={pending}
          day={formatRunDay(addDays(date, trip.day_offset), locale)}
          name={ownName ? trip.name : undefined}
          focusable={!ghost}
          t={t}
        />
      </div>
    );
  };

  // Nothing chosen yet: the first run after the chosen time, so the route beside the list is
  // never empty.
  const firstRun =
    slots.find((slot) => slot.trip && slot.position >= 0) ?? slots.find((s) => s.trip);
  useEffect(() => {
    if (submitted && !chosen && firstRun) choose(runChoice(firstRun), false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submitted, chosen, firstRun?.position, firstRun?.trip?.train_id]);

  // A station needs only a date and a time; a line or train needs all five.
  const canSubmit = selectedStation
    ? Boolean(date) && Boolean(time)
    : selectedLine !== null &&
      direction !== undefined &&
      stationId !== undefined &&
      Boolean(date) &&
      Boolean(time);

  const submit = () => {
    if (selectedStation) {
      setBoardFor({ id: selectedStation.id, date, time });
      return;
    }
    setSubmitted(true);
  };

  // A station from the address: its board opens under the form, for the date and time in the
  // address or, without them, for now.
  useEffect(() => {
    if (!initialStation) return;
    setSelectedLine(null);
    setSelectedStation({ id: initialStation.id, name: "" });
    setSelectedLabel("");
    setQuery("");
    setDirection(undefined);
    setStationId(undefined);
    setSubmitted(false);
    setChosen(null);
    setStopBoard(null);
    if (initialStation.date && initialStation.time) {
      setDate(initialStation.date);
      setTime(initialStation.time);
    }
    setBoardFor({
      id: initialStation.id,
      date: initialStation.date,
      time: initialStation.time,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialStation?.id, initialStation?.date, initialStation?.time]);
  // The board knows the station's name; the search field and the form take it once it does.
  const nameStation = (name: string) => {
    setSelectedStation((current) =>
      current && current.name === "" ? { ...current, name } : current,
    );
    setSelectedLabel((label) => label || name);
    setQuery((current) => current || name);
  };

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
  // route grouped around it), never a way to skip the gate. A station keeps the
  // user here too: it needs only a date and a time, then opens its board for
  // that moment.
  const chooseSuggestion = async (result: SuggestResult) => {
    setSuggestionsOpen(false);
    setDirection(undefined);
    setStationId(undefined);
    withdraw();
    setSelectedLabel(result.name);

    if (result.kind === "station") {
      setSelectedLine(null);
      setSelectedStation({ id: result.id, name: result.name });
      setQuery(result.name);
      return;
    }
    setSelectedStation(null);

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

  const stopName = stations.find((s) => s.id === stationId)?.name ?? t.selectedStop;
  const flowChosen = selectedLine !== null || selectedStation !== null;
  const directionLabel = directions.find((d) => d.direction === direction)?.label;
  const DirectionIcon = icons.direction;
  const Train = icons.train;
  const Stop = icons.stop;

  return (
    <div>
      <Card className="rounded-lg border border-line bg-panel p-4 text-paper shadow-none sm:p-5">
        {/* Two rows on wide screens: where and what (Bundesland, a wide search box), then
            direction, stop, date and time. */}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <FieldLabel kind="state" htmlFor="train-state" className="mb-1.5">
              {t.stateLabel}
            </FieldLabel>
            <select
              id="train-state"
              value={region}
              onChange={(event) => {
                const next = event.target.value;
                chooseRegion(isStateCode(next) ? next : "");
              }}
              className={FIELD_CLASS}
            >
              <option value="">{t.allStates}</option>
              {statesFor(locale).map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
          <div className="relative lg:col-span-3" ref={searchFieldRef}>
            <FieldLabel kind="search" htmlFor="train-search" className="mb-1.5">
              {t.trainLabel}
            </FieldLabel>
            <div className="flex h-[46px] items-center gap-2 rounded-md border border-line bg-ink px-3 focus-within:border-teal">
              <icons.search className="size-4 shrink-0 text-fog" aria-hidden="true" />
              <input
                id="train-search"
                value={query}
                onFocus={() => setSuggestionsOpen(Boolean(query.trim()))}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setSelectedLine(null);
                  setSelectedStation(null);
                  setSelectedLabel("");
                  setDirection(undefined);
                  setStationId(undefined);
                  withdraw();
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
            <FieldLabel kind="direction" htmlFor="train-direction" className="mb-1.5">
              {t.direction}
            </FieldLabel>
            <select
              id="train-direction"
              value={direction ?? ""}
              onChange={(event) => {
                setDirection(event.target.value === "" ? undefined : Number(event.target.value));
                setStationId(undefined);
                withdraw();
              }}
              disabled={!selectedLine || directions.length === 0}
              className={FIELD_CLASS}
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
            <FieldLabel kind="stop" htmlFor="train-stop" className="mb-1.5">
              {t.selectedStop}
            </FieldLabel>
            <select
              id="train-stop"
              value={stationId ?? ""}
              onChange={(event) => {
                setStationId(event.target.value === "" ? undefined : Number(event.target.value));
                withdraw();
              }}
              disabled={stations.length === 0}
              className={FIELD_CLASS}
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
            <FieldLabel kind="date" htmlFor="train-date" className="mb-1.5">
              {t.searchDate}
            </FieldLabel>
            <input
              id="train-date"
              type="date"
              value={date}
              onChange={(event) => {
                setDate(event.target.value);
                withdraw();
              }}
              disabled={!flowChosen}
              className={FIELD_CLASS}
            />
          </div>
          <button
            type="button"
            disabled={!flowChosen}
            onClick={() => {
              setDate(todayDate());
              withdraw();
            }}
            className={`${QUICK_CLASS} sm:col-start-1 sm:row-start-4 lg:col-start-3 lg:row-start-3`}
          >
            {t.today}
          </button>
          <div>
            <FieldLabel kind="time" htmlFor="train-time" className="mb-1.5">
              {t.searchTime}
            </FieldLabel>
            <input
              id="train-time"
              type="time"
              value={time}
              onChange={(event) => {
                setTime(event.target.value);
                withdraw();
              }}
              disabled={!flowChosen}
              className={FIELD_CLASS}
            />
          </div>
          <button
            type="button"
            disabled={!flowChosen}
            onClick={() => {
              setTime(nowTime());
              withdraw();
            }}
            className={`${QUICK_CLASS} sm:col-start-2 sm:row-start-4 lg:col-start-4 lg:row-start-3`}
          >
            {t.now}
          </button>
          <Button
            onClick={submit}
            disabled={!canSubmit}
            className="h-[42px] w-full rounded-md bg-teal font-display font-semibold text-action-foreground hover:bg-teal disabled:opacity-40 sm:col-span-2 sm:row-start-5 lg:col-span-1 lg:col-start-1 lg:row-start-3"
          >
            {selectedStation ? t.showBoard : t.getPrognose}
          </Button>
        </div>
      </Card>

      {!submitted && boardFor === null && flowChosen && (
        <Alert className="mt-4 border-line bg-panel">
          <AlertDescription className="text-center text-xs text-fog">
            {selectedStation ? t.pickStationFields : t.pickAllFields}
          </AlertDescription>
        </Alert>
      )}

      {(boardFor || submitted) && stopBoard && (
        <div ref={resultsRef} className="mt-6 scroll-mt-4">
          <BoardV2
            key={`${stopBoard.id}-${stopBoard.date}-${stopBoard.time}`}
            stationId={stopBoard.id}
            date={stopBoard.date}
            time={stopBoard.time}
            locale={locale}
            onBack={() => showStopBoard(null)}
            fromRun={stopBoard.fromRun}
            embedded
            className={RESULT_HEIGHT}
          />
        </div>
      )}

      {(boardFor || submitted) && !stopBoard && (
        // The list on the left and the chosen run's route on the right, as tall as the list (the
        // route has no chevrons, so it needs less width); on a phone and tablet the route comes
        // under the list.
        <div
          ref={resultsRef}
          className="mt-6 scroll-mt-4 lg:grid lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:gap-6"
        >
          <div className="min-w-0">
            {boardFor && (
              <BoardV2
                stationId={boardFor.id}
                date={boardFor.date}
                time={boardFor.time}
                locale={locale}
                onName={nameStation}
                chosen={chosen}
                onChoose={choose}
                embedded
                className={RESULT_HEIGHT}
              />
            )}

            {submitted && (
              <Card
                className="rounded-lg border border-line bg-panel p-5 text-paper shadow-none sm:p-7"
                aria-live="polite"
              >
                <div className="flex items-center gap-3 border-b border-line pb-4">
                  <span className="grid size-9 shrink-0 place-items-center rounded-md bg-ink text-teal">
                    <Train className="size-4" />
                  </span>
                  <div className="min-w-0">
                    <h2 className="font-display text-lg font-semibold text-paper">
                      {selectedLabel || query}
                    </h2>
                    <p className="mt-0.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13.67px] text-fog">
                      {directionLabel && (
                        <span className="inline-flex items-center gap-1.5" title={t.direction}>
                          <DirectionIcon className="size-3.5 shrink-0" aria-hidden="true" />
                          {directionLabel}
                        </span>
                      )}
                      <span className="inline-flex items-center gap-1.5" title={t.selectedStop}>
                        <Stop className="size-3.5 shrink-0" aria-hidden="true" />
                        {stopName}
                      </span>
                    </p>
                  </div>
                </div>
                {/* The runs, with Earlier/Later on their left from a tablet up and below them on a
                    phone. */}
                <div className="pt-3 sm:flex sm:items-stretch sm:gap-5">
                  <div className="min-w-0 flex-1">
                    <RunColumnHeader t={t} />
                    <div className="relative overflow-hidden">
                      <div ref={cassetteRef} className="relative">
                        <div ref={listRef} className="divide-y divide-line">
                          {isLoading ? (
                            <div aria-label={t.loading}>
                              {Array.from({ length: WINDOW_SIZE }).map((_, i) => (
                                <div key={i} className={`${RUN_ROW_HEIGHT} px-2 py-2 sm:px-3`}>
                                  <Skeleton className="h-full w-full bg-line" />
                                </div>
                              ))}
                            </div>
                          ) : isError ? (
                            <p className="px-1 py-8 text-center text-sm text-fog">{t.error}</p>
                          ) : slots.length > 0 ? (
                            slots.map((slot) => runRow(slot, false))
                          ) : (
                            <p className="px-1 py-8 text-center text-sm text-fog">
                              {t.nothingScheduled}
                            </p>
                          )}
                        </div>
                        {/* Outside the divided list: as a child of it, the runs that left would give the
                      last real run a divider line for the length of the animation. */}
                        {moving && moving.leaving.length > 0 && (
                          <div
                            ref={ghostsRef}
                            className={`pointer-events-none absolute inset-x-0 ${
                              moving.toEarlier ? "top-full" : "bottom-full"
                            }`}
                          >
                            {moving.leaving.map((slot) => runRow(slot, true))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  {slots.length > 0 && (
                    <div className="mt-3 flex gap-3 border-t border-line pt-4 sm:order-first sm:mt-0 sm:w-28 sm:shrink-0 sm:flex-col sm:border-t-0 sm:border-r sm:pt-9 sm:pr-5">
                      <Button
                        variant="outline"
                        onClick={() => step(true)}
                        disabled={!canEarlier || stepping}
                        className="h-[42px] flex-1 gap-2 border-line bg-ink text-paper hover:bg-panel disabled:opacity-40 sm:h-auto sm:w-full sm:flex-col sm:justify-center"
                      >
                        <ChevronUp className="size-4" />
                        {t.showEarlier}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => step(false)}
                        disabled={!canLater || stepping}
                        className="h-[42px] flex-1 gap-2 border-line bg-ink text-paper hover:bg-panel disabled:opacity-40 sm:h-auto sm:w-full sm:flex-col sm:justify-center"
                      >
                        <ChevronDown className="size-4" />
                        {t.showLater}
                      </Button>
                    </div>
                  )}
                </div>
              </Card>
            )}
          </div>
          <div ref={routeRef} className="relative mt-6 min-w-0 scroll-mt-4 lg:mt-0">
            {chosen ? (
              <TrainV2
                key={`${chosen.trainId}-${chosen.date}`}
                trainId={chosen.trainId}
                stationId={chosen.stationId}
                date={chosen.date}
                locale={locale}
                embedded
                onStop={({ stationId: id, date: day, time: at }) =>
                  showStopBoard({
                    id,
                    date: day,
                    time: at,
                    fromRun: { trainId: chosen.trainId, date: chosen.date },
                  })
                }
                className={ROUTE_HEIGHT}
              />
            ) : (
              <Card
                className={`grid place-items-center rounded-lg border border-dashed border-line bg-panel p-7 text-center text-sm text-fog shadow-none ${ROUTE_HEIGHT}`}
              >
                {t.chooseRun}
              </Card>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
