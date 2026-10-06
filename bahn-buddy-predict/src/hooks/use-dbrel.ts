import { keepPreviousData, useQuery } from "@tanstack/react-query";
import * as api from "@/lib/api";

// Matches dbrel.web.queries.MIN_QUERY_LEN -- below this the backend returns
// [] without even scanning, so there's nothing to gain from querying sooner.
const MIN_QUERY_LEN = 2;

export function useSuggest(query: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["suggest", trimmed],
    queryFn: () => api.suggest(trimmed),
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

export function useTrainRoute(trainId: number | undefined, stationId?: number) {
  return useQuery({
    queryKey: ["train-route", trainId, stationId],
    queryFn: () => api.trainRoute(trainId as number, stationId),
    enabled: trainId !== undefined,
  });
}

export function useStationBoard(
  stationId: number | undefined,
  opts?: { backMin?: number; fwdMin?: number },
) {
  return useQuery({
    queryKey: ["station-board", stationId, opts?.backMin, opts?.fwdMin],
    queryFn: () => api.stationBoard(stationId as number, opts),
    enabled: stationId !== undefined,
    refetchInterval: 60_000,
  });
}

export function useLineDetail(lineId: number | undefined) {
  return useQuery({
    queryKey: ["line-detail", lineId],
    queryFn: () => api.lineDetail(lineId as number),
    enabled: lineId !== undefined,
  });
}
