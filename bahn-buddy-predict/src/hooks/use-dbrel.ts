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

export function useLineDirections(lineId: number | undefined) {
  return useQuery({
    queryKey: ["line-directions", lineId],
    queryFn: () => api.lineDirections(lineId as number),
    enabled: lineId !== undefined,
  });
}

export function useLineStations(lineId: number | undefined, direction: 0 | 1 | undefined) {
  return useQuery({
    queryKey: ["line-stations", lineId, direction],
    queryFn: () => api.lineStations(lineId as number, direction as 0 | 1),
    enabled: lineId !== undefined && direction !== undefined,
  });
}

export function useTripsAround(
  lineId: number | undefined,
  direction: 0 | 1 | undefined,
  stationId: number | undefined,
  date: string,
  time: string,
  submitted: boolean,
) {
  return useQuery({
    queryKey: ["trips-around", lineId, direction, stationId, date, time],
    queryFn: () =>
      api.tripsAround(lineId as number, direction as 0 | 1, stationId as number, date, time),
    // submitted: the request only ever fires from an explicit button press,
    // never just because every field happens to be filled in.
    enabled:
      submitted &&
      lineId !== undefined &&
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
