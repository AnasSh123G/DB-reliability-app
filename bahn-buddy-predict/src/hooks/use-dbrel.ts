import { keepPreviousData, useQueries, useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import * as api from "@/lib/api";

// Matches dbrel.web.queries.MIN_QUERY_LEN -- below this the backend returns
// [] without even scanning, so there's nothing to gain from querying sooner.
const MIN_QUERY_LEN = 2;

/**
 * Search results for `query`, narrowed to the lines and trains of `state` when one is given.
 *
 * @param query - what the user typed.
 * @param state - a Bundesland code, or undefined for all of Germany.
 * @returns the react-query result holding the matches.
 */
export function useSuggest(query: string, state?: string, only?: "station" | "line" | "train") {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["suggest", trimmed, state ?? "", only ?? ""],
    queryFn: () => api.suggest(trimmed, only, state),
    enabled: trimmed.length >= MIN_QUERY_LEN,
    staleTime: 30_000,
  });
}

export function useRouteDirections(ref: api.RouteRef | undefined) {
  return useQuery({
    queryKey: ["route-directions", ref?.kind, ref?.id],
    queryFn: () => api.routeDirections(ref as api.RouteRef),
    enabled: ref !== undefined,
  });
}

export function useRouteStations(ref: api.RouteRef | undefined, direction: 0 | 1 | undefined) {
  return useQuery({
    queryKey: ["route-stations", ref?.kind, ref?.id, direction],
    queryFn: () => api.routeStations(ref as api.RouteRef, direction as 0 | 1),
    enabled: ref !== undefined && direction !== undefined,
  });
}

/**
 * The runs around a chosen time, for a window slid `shift` runs from the chosen time.
 *
 * The window is always 3 runs: shift 0 is the 2 before and 1 after, shift -1 is the 3 before the
 * chosen time, shift 1 is the 1 before and 2 after. The server is asked for every run the slid
 * window needs; the result carries the `shift` it was fetched for, and the previous result stays
 * on screen while the next one loads so the page can animate from one to the other.
 */
export function useRouteTripsAround(
  ref: api.RouteRef | undefined,
  direction: 0 | 1 | undefined,
  stationId: number | undefined,
  date: string,
  time: string,
  submitted: boolean,
  shift = 0,
) {
  const before = 2 + Math.max(0, -shift);
  const after = 1 + Math.max(0, shift);
  return useQuery({
    queryKey: ["route-trips-around", ref?.kind, ref?.id, direction, stationId, date, time, shift],
    queryFn: async () => ({
      ...(await api.routeTripsAround(
        ref as api.RouteRef,
        direction as 0 | 1,
        stationId as number,
        date,
        time,
        before,
        after,
      )),
      shift,
    }),
    placeholderData: keepPreviousData,
    // submitted: the request only ever fires from an explicit button press,
    // never just because every field happens to be filled in.
    enabled:
      submitted &&
      ref !== undefined &&
      direction !== undefined &&
      stationId !== undefined &&
      Boolean(date) &&
      Boolean(time),
  });
}

export function useTrainRoute(trainId: number | undefined, stationId?: number, date?: string) {
  return useQuery({
    queryKey: ["train-route", trainId, stationId, date],
    queryFn: () => api.trainRoute(trainId as number, stationId, date),
    enabled: trainId !== undefined,
  });
}

/**
 * A station's board: live (window around now, refreshed every minute), or, with `date` and
 * `time`, around that moment, which does not change and so is not refreshed.
 */
export function useStationBoard(
  stationId: number | undefined,
  opts?: { backMin?: number; fwdMin?: number; date?: string; time?: string },
) {
  const chosen = Boolean(opts?.date && opts?.time);
  return useQuery({
    queryKey: [
      "station-board",
      stationId,
      opts?.backMin,
      opts?.fwdMin,
      opts?.date ?? "",
      opts?.time ?? "",
    ],
    queryFn: () => api.stationBoard(stationId as number, opts),
    enabled: stationId !== undefined,
    refetchInterval: chosen ? false : 60_000,
  });
}

export function useLineDetail(lineId: number | undefined) {
  return useQuery({
    queryKey: ["line-detail", lineId],
    queryFn: () => api.lineDetail(lineId as number),
    enabled: lineId !== undefined,
  });
}

/** A run of positions asked for from the server: `count` runs from `offset` (see api.routeTripsChunk). */
export interface ChunkSpec {
  offset: number;
  count: number;
}

/** The runs a chunk request has returned. */
export interface LoadedChunk extends ChunkSpec {
  trips: api.TripAround[];
}

/** The window the page opens on: the 2 runs at or before the chosen time and the 4 after it. */
export const FIRST_CHUNK: ChunkSpec = { offset: -2, count: 6 };

/**
 * The runs at a stop around a chosen time, fetched in chunks that are each requested once.
 *
 * The first chunk is the 3 runs the page opens on; more chunks are added with `request`, and each
 * is cached by its own range, so asking for a neighbouring range never fetches a loaded run
 * again. All chunks are dropped when the route, direction, stop, date or time changes.
 *
 * @param ref - the line or route.
 * @param direction - 0 or 1.
 * @param stationId - the stop.
 * @param date - YYYY-MM-DD.
 * @param time - HH:MM.
 * @param submitted - false until the Prognose button was pressed; nothing is fetched before.
 * @returns the loaded chunks in the order requested, every range asked for so far, the stop's
 *   number of distinct runs and the model stamp (both from the first chunk), whether the first
 *   chunk is still loading or failed, and `request` to add a chunk.
 */
export function useTripChunks(
  ref: api.RouteRef | undefined,
  direction: 0 | 1 | undefined,
  stationId: number | undefined,
  date: string,
  time: string,
  submitted: boolean,
) {
  const scope = [ref?.kind, ref?.id, direction, stationId, date, time].join("|");
  const [added, setAdded] = useState<{ scope: string; chunks: ChunkSpec[] }>({
    scope,
    chunks: [],
  });
  const extra = added.scope === scope ? added.chunks : [];
  const requested = [FIRST_CHUNK, ...extra];

  const request = useCallback(
    (offset: number, count: number) => {
      setAdded((prev) => {
        const base = prev.scope === scope ? prev.chunks : [];
        if (base.some((c) => c.offset === offset && c.count === count)) return prev;
        return { scope, chunks: [...base, { offset, count }] };
      });
    },
    [scope],
  );

  const enabled =
    submitted &&
    ref !== undefined &&
    direction !== undefined &&
    stationId !== undefined &&
    Boolean(date) &&
    Boolean(time);
  const results = useQueries({
    queries: requested.map((chunk) => ({
      queryKey: [
        "route-trips-chunk",
        ref?.kind,
        ref?.id,
        direction,
        stationId,
        date,
        time,
        chunk.offset,
        chunk.count,
      ],
      queryFn: () =>
        api.routeTripsChunk(
          ref as api.RouteRef,
          direction as 0 | 1,
          stationId as number,
          date,
          time,
          chunk.offset,
          chunk.count,
        ),
      enabled,
      staleTime: 5 * 60_000,
    })),
  });

  const chunks: LoadedChunk[] = [];
  results.forEach((result, i) => {
    const spec = requested[i];
    if (spec && result.data) chunks.push({ ...spec, trips: result.data.trips });
  });
  const first = results[0];
  return {
    chunks,
    requested,
    request,
    nCalls: first?.data?.n_calls,
    modelStamp: first?.data?.model_stamp,
    isLoading: enabled && (first?.isLoading ?? true),
    isError: first?.isError ?? false,
  };
}

/** Fetch only a submitted planner request; editing form fields does not start a search. */
export function usePlan(params: api.PlanParams | undefined) {
  return useQuery({
    queryKey: ["plan", params?.from, params?.to, params?.date, params?.time, params?.before],
    queryFn: () => {
      if (!params) throw new Error("Planner parameters are required");
      return api.plan(params.from, params.to, params.date, params.time, params.before);
    },
    enabled: params !== undefined,
    staleTime: 5 * 60_000,
    retry: false,
  });
}
