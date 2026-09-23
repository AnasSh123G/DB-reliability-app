// Typed client for dbrel's JSON API (src/dbrel/web/{views,queries}.py). Every
// request is relative -- in production the reverse proxy in front of dbrel
// routes /api/* to the Django process this app's own server never talks to
// directly (see design.md decision 6, replace-web-ui-with-bahn-buddy).

export interface DelayPred {
  p50_min: number;
  lo_min: number;
  hi_min: number;
}

export interface SuggestResult {
  kind: "station" | "line" | "train";
  id: number;
  name: string;
  sub: string;
  url: string;
  stops?: string | null;
  eva?: number | null;
  tracked?: boolean;
  replacement_bus?: boolean;
}

export interface SuggestResponse {
  query: string;
  results: SuggestResult[];
}

export interface StationBoardCall {
  station_stop_id: number;
  train_id: number;
  terminates: boolean;
  headline: string;
  train: string;
  category: string | null;
  segment: string | null;
  number: number | null;
  platform: string | null;
  origin: string | null;
  destination: string | null;
  service_date: string;
  arr: string | null;
  dep: string | null;
  arr_pred: DelayPred | null;
  dep_pred: DelayPred | null;
}

export interface StationBoard {
  station: { id: number; name: string; eva: number | null; tracked: boolean };
  window: { from: string | null; to: string | null; date: string };
  calls: StationBoardCall[];
}

export interface TrainRouteStop {
  seq: number;
  name: string;
  monitored: boolean;
  station_id: number | null;
  arr: string | null;
  dep: string | null;
  platform: string | null;
  arr_pred: DelayPred | null;
  dep_pred: DelayPred | null;
  url?: string;
}

export interface TrainRoute {
  train: {
    id: number;
    name: string;
    category: string | null;
    segment: string | null;
    number: number | null;
    operator: string | null;
    origin: string | null;
    destination: string | null;
    n_stops: number;
    line: { id: number; name: string } | null;
    replacement_bus: boolean;
  };
  n_monitored: number;
  stops: TrainRouteStop[];
}

export interface LineDirection {
  direction: 0 | 1;
  label: string;
  n_runs: number;
}

export interface LineDirectionsResponse {
  line: { id: number; name: string };
  directions: LineDirection[];
}

export interface LineStation {
  id: number;
  name: string;
}

export interface TripAround {
  train_id: number;
  name: string;
  url: string;
  sched_arr: string | null;
  sched_dep: string | null;
  arr_pred: DelayPred | null;
  dep_pred: DelayPred | null;
  relation: "before" | "after";
}

export interface TripsAroundResponse {
  line: { id: number; name: string };
  direction: 0 | 1;
  station: { id: number; name: string };
  trips: TripAround[];
  date?: string;
  day_type?: string;
}

export interface LineRun {
  id: number;
  name: string;
  number: number | null;
  segment: string | null;
  origin: string | null;
  destination: string | null;
  departs: string | null;
  n_stops: number | null;
  n_monitored: number;
  url: string;
  partial: boolean;
  current: boolean;
  pred: DelayPred | null;
}

export interface LineDetail {
  line: {
    id: number;
    name: string;
    category: string | null;
    operator: string | null;
    replacement_bus: boolean;
  };
  n_runs: number;
  directions: { label: string; n_runs: number; runs: LineRun[] }[];
}

class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function apiFetch<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  let query = "";
  if (params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) search.set(key, String(value));
    }
    const qs = search.toString();
    if (qs) query = `?${qs}`;
  }
  const res = await fetch(`${path}${query}`);
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { error?: string } | null;
    throw new ApiError(body?.error ?? `${path} failed (${res.status})`, res.status);
  }
  return res.json() as Promise<T>;
}

export function suggest(query: string, only?: "station" | "line" | "train") {
  return apiFetch<SuggestResponse>("/api/suggest", { q: query, only });
}

export function stationBoard(stationId: number, opts?: { backMin?: number; fwdMin?: number }) {
  return apiFetch<StationBoard>(`/api/station/${stationId}/board`, {
    back: opts?.backMin,
    fwd: opts?.fwdMin,
  });
}

export function trainRoute(trainId: number) {
  return apiFetch<TrainRoute>(`/api/train/${trainId}/route`);
}

export function lineDirections(lineId: number) {
  return apiFetch<LineDirectionsResponse>(`/api/line/${lineId}/directions`);
}

export function lineStations(lineId: number, direction: 0 | 1) {
  return apiFetch<{ stations: LineStation[] }>(`/api/line/${lineId}/stations`, { direction });
}

export function tripsAround(
  lineId: number,
  direction: 0 | 1,
  stationId: number,
  date: string,
  time: string,
) {
  return apiFetch<TripsAroundResponse>(`/api/line/${lineId}/around`, {
    direction,
    station: stationId,
    date,
    time,
  });
}

export function lineDetail(lineId: number) {
  return apiFetch<LineDetail>(`/api/line/${lineId}/detail`);
}
