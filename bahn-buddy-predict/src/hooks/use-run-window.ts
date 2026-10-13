import { useEffect, useMemo, useState } from "react";
import { FIRST_CHUNK, useTripChunks } from "@/hooks/use-dbrel";
import { useModelPredictionGroups } from "@/hooks/use-tabm";
import type { RouteRef, TripAround } from "@/lib/api";

/** How many runs the card shows at once, and how far one Earlier/Later press moves it. */
export const WINDOW_SIZE = 3;
/** How many runs one background request fetches. */
const CHUNK_SIZE = 12;
/** A further chunk is fetched once the window is this many runs from the loaded edge. */
const FETCH_AHEAD = 6;

export interface RunSlot {
  /** Position of the run: 0 is the first run after the chosen time, -1 the last at or before it. */
  position: number;
  /** The run, or undefined while its chunk is still loading. */
  trip: TripAround | undefined;
}

/**
 * The window of runs the Prognose card shows, stepped three at a time over runs that are fetched
 * and forecast ahead of the user.
 *
 * The 3 runs the page opens on are fetched and forecast first; once their forecasts are in, the
 * runs before and after them are fetched and forecast in the background. When the window comes
 * within 6 runs of the loaded edge in either direction, the next chunk that way is fetched. The
 * window never reaches an end: past the day's last run come the next day's runs, and before its
 * first the previous day's, however few runs a day the stop has.
 *
 * @param ref - the line or route.
 * @param direction - 0 or 1.
 * @param stationId - the stop.
 * @param date - YYYY-MM-DD.
 * @param time - HH:MM.
 * @param submitted - false until Prognose was pressed; the window then goes back to the start.
 * @param initialStart - the position of the window's first run, to come back to a result.
 * @returns the slots in view, how many distinct runs the stop has, the position of the window,
 *   whether it can step either way, `step`, every forecast made so far (by `runKey`) and the
 *   loading state of the first chunk.
 */
export function useRunWindow(
  ref: RouteRef | undefined,
  direction: 0 | 1 | undefined,
  stationId: number | undefined,
  date: string,
  time: string,
  submitted: boolean,
  initialStart = FIRST_CHUNK.offset,
) {
  const { chunks, requested, request, nCalls, modelStamp, isLoading, isError } = useTripChunks(
    ref,
    direction,
    stationId,
    date,
    time,
    submitted,
  );
  const n = nCalls ?? 0;

  const [start, setStart] = useState(initialStart);
  useEffect(() => {
    if (!submitted) setStart(FIRST_CHUNK.offset);
  }, [submitted]);

  // The timetable repeats every day, so positions go on forever: past the last run of the day come
  // the first runs of the next day, and before the first the last runs of the day before. Every
  // position is its own run on its own day, so it has its own forecast.
  const { byPosition, groups } = useMemo(() => {
    const byPosition = new Map<number, TripAround>();
    const groups: { key: number; model_input: TripAround["model_input"] }[][] = [];
    for (const chunk of chunks) {
      for (const trip of chunk.trips) byPosition.set(trip.position, trip);
      groups.push(
        chunk.trips.map((trip) => ({ key: trip.position, model_input: trip.model_input })),
      );
    }
    return { byPosition, groups };
  }, [chunks]);

  const { predictions, firstDone } = useModelPredictionGroups(modelStamp, groups);

  const canStep = n > 0;
  const slots: RunSlot[] = [];
  if (n > 0) {
    for (let i = 0; i < WINDOW_SIZE; i++) {
      const position = start + i;
      slots.push({ position, trip: byPosition.get(position) });
    }
  }

  // Fetch ahead, in both directions without end.
  const low = Math.min(...requested.map((c) => c.offset));
  const high = Math.max(...requested.map((c) => c.offset + c.count - 1));
  const firstLoaded = chunks.some((c) => c.offset === FIRST_CHUNK.offset);
  useEffect(() => {
    if (n === 0 || !firstLoaded) return;
    if (requested.length === 1) {
      // Opening: once the 3 shown are forecast, the runs after and before them.
      if (!firstDone) return;
      request(high + 1, CHUNK_SIZE);
      request(low - CHUNK_SIZE, CHUNK_SIZE);
      return;
    }
    if (high - (start + WINDOW_SIZE - 1) <= FETCH_AHEAD) request(high + 1, CHUNK_SIZE);
    if (start - low <= FETCH_AHEAD) request(low - CHUNK_SIZE, CHUNK_SIZE);
  }, [n, firstLoaded, firstDone, requested.length, high, low, start, request]);

  return {
    slots,
    nCalls: n,
    start,
    canEarlier: canStep,
    canLater: canStep,
    step: (toEarlier: boolean) => setStart(start + (toEarlier ? -WINDOW_SIZE : WINDOW_SIZE)),
    predictions,
    isLoading,
    isError,
  };
}
