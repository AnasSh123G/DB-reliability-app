import { useQuery } from "@tanstack/react-query";
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

export function useRouteTripsAround(
  ref: api.RouteRef | undefined,
  direction: 0 | 1 | undefined,
  stationId: number | undefined,
  date: string,
  time: string,
  submitted: boolean,
) {
  return useQuery({
    queryKey: ["route-trips-around", ref?.kind, ref?.id, direction, stationId, date, time],
    queryFn: () =>
      api.routeTripsAround(ref as api.RouteRef, direction as 0 | 1, stationId as number, date, time),
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

export function useTrainRoute(trainId: number | undefined) {
  return useQuery({
    queryKey: ["train-route", trainId],
    queryFn: () => api.trainRoute(trainId as number),
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
