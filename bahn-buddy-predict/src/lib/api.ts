// Typed client for dbrel's JSON API (src/dbrel/web/{views,queries}.py). Every
// request is relative -- in production the reverse proxy in front of dbrel
// routes /api/* to the Django process this app's own server never talks to
// directly (see design.md decision 6, replace-web-ui-with-bahn-buddy).

export interface DelayPred {
  p50_min: number;
  lo_min: number;
  hi_min: number;
}

// One call's already-encoded input for the current delay model (see
// web/queries.py's _model_input) -- the exact `cat`/`num` tensors the ONNX
// model expects. Never run through the model server-side; the browser does
// that (see lib/tabm.ts). One input covers both arr and dep, which side's
// prediction is shown is gated on whether that side has a scheduled time.
export interface ModelInput {
  cat: number[];
  num: number[];
}

export interface SuggestResult {
  kind: "station" | "line" | "train";
  id: number;
  name: string;
  number_name?: string;
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
  number_name: string;
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
  model_input: ModelInput | null;
}

export interface StationBoard {
  station: { id: number; name: string; eva: number | null; tracked: boolean };
  window: { from: string | null; to: string | null; date: string };
  model_stamp: string | null;
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
  model_input: ModelInput | null;
  url?: string;
}

export interface TrainRoute {
  train: {
    id: number;
    name: string;
    number_name: string;
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
  model_stamp: string | null;
  stops: TrainRouteStop[];
  /** Present for a train not in the timetable for 14 days before the newest data; then no stops. */
  retired?: { last_seen_date: string | null };
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
  number_name: string;
  url: string;
  sched_arr: string | null;
  sched_dep: string | null;
  arr_pred: DelayPred | null;
  dep_pred: DelayPred | null;
  model_input: ModelInput | null;
  relation: "before" | "after";
  /** 0 is the first run after the chosen time, -1 the last at or before it, and so on. */
  position: number;
  /** Which day the run is on, counted from the chosen date: 0 that day, 1 the next, -1 the one before. */
  day_offset: number;
}

export interface TripsAroundResponse {
  line: { id: number; name: string };
  direction: 0 | 1;
  station: { id: number; name: string };
  model_stamp: string | null;
  /** How many distinct runs the stop has, so a window knows how far it can slide. */
  n_calls?: number;
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
  model_input: ModelInput | null;
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
  model_stamp: string | null;
  directions: { label: string; n_runs: number; runs: LineRun[] }[];
}

export class ApiError extends Error {
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

/**
 * Stations, lines and trains whose name starts with `query`.
 *
 * @param query - what the user typed.
 * @param only - limit the results to one kind.
 * @param state - a Bundesland code (e.g. "DE-NW"): only lines and trains that stop there.
 * @returns the server's matches.
 */
export function suggest(query: string, only?: "station" | "line" | "train", state?: string) {
  return apiFetch<SuggestResponse>("/api/suggest", { q: query, only, state });
}

/**
 * The calls at a station in a window around now, or around a chosen date and time.
 *
 * @param stationId - the station.
 * @param opts - minutes to widen the window back and forward, and the moment to centre it on
 *   (`date` YYYY-MM-DD and `time` HH:MM, both or neither).
 * @returns the board.
 */
export function stationBoard(
  stationId: number,
  opts?: { backMin?: number; fwdMin?: number; date?: string; time?: string },
) {
  return apiFetch<StationBoard>(`/api/station/${stationId}/board`, {
    back: opts?.backMin,
    fwd: opts?.fwdMin,
    date: opts?.date,
    time: opts?.time,
  });
}

export function trainRoute(trainId: number, stationId?: number) {
  return apiFetch<TrainRoute>(`/api/train/${trainId}/route`, { station: stationId });
}

// What the direction/stop/Prognose flow runs on: a real line, or -- for a
// train published without one (ICE, IC, ...) -- the route the backend groups
// around that train, keyed by the train's id.
export interface RouteRef {
  kind: "line" | "corridor";
  id: number;
}

function routeBase(ref: RouteRef) {
  return `/api/${ref.kind}/${ref.id}`;
}

export function routeDirections(ref: RouteRef) {
  return apiFetch<LineDirectionsResponse>(`${routeBase(ref)}/directions`);
}

export function routeStations(ref: RouteRef, direction: 0 | 1) {
  return apiFetch<{ stations: LineStation[] }>(`${routeBase(ref)}/stations`, { direction });
}

export function routeTripsAround(
  ref: RouteRef,
  direction: 0 | 1,
  stationId: number,
  date: string,
  time: string,
  before = 2,
  after = 1,
) {
  return apiFetch<TripsAroundResponse>(`${routeBase(ref)}/around`, {
    direction,
    station: stationId,
    date,
    time,
    before,
    after,
  });
}

/**
 * `count` runs at a stop, starting `offset` runs from the chosen time.
 *
 * @param ref - the line or route.
 * @param direction - 0 or 1.
 * @param stationId - the stop.
 * @param date - YYYY-MM-DD.
 * @param time - HH:MM.
 * @param offset - position of the first run wanted: 0 is the first run after the time, -1 the
 *   last at or before it.
 * @param count - how many runs.
 * @returns the runs, each with its `position`, and how many distinct runs the stop has.
 */
export function routeTripsChunk(
  ref: RouteRef,
  direction: 0 | 1,
  stationId: number,
  date: string,
  time: string,
  offset: number,
  count: number,
) {
  return apiFetch<TripsAroundResponse>(`${routeBase(ref)}/around`, {
    direction,
    station: stationId,
    date,
    time,
    offset,
    count,
  });
}

export function lineDetail(lineId: number) {
  return apiFetch<LineDetail>(`/api/line/${lineId}/detail`);
}

/** A local Berlin clock time and its unambiguous instant. */
export interface Moment {
  epoch: number;
  date: string;
  time: string;
}

export interface PlanForecast {
  p25_min: number;
  p50_min: number;
  p75_min: number;
}

export interface PlanStation {
  id: number;
  name: string;
}

export interface PlanLeg {
  train_id: number;
  name: string;
  number_name: string;
  category: string | null;
  line_key: string;
  from: PlanStation;
  to: PlanStation;
  departure: Moment;
  arrival: Moment;
  expected_arrival: Moment;
  forecast: PlanForecast | null;
  n_stops: number;
}

/** Chance of missing a change: under 25%, 25-50%, 50-75%, over 75%. */
export type MissChance = "low" | "medium" | "high" | "very_high";

export interface PlanChange {
  station: PlanStation;
  planned_min: number;
  left_min: number;
  at_risk: boolean;
  miss_chance: MissChance | null;
}

export interface Journey {
  route_key: string;
  departure: Moment;
  arrival: Moment;
  expected_arrival: Moment;
  delay_min: number;
  forecast: PlanForecast | null;
  duration_min: number;
  n_changes: number;
  at_risk: boolean;
  /** The worst change's chance; null for a direct journey or without forecasts. */
  miss_chance: MissChance | null;
  legs: PlanLeg[];
  changes: PlanChange[];
}

export interface PlanStats {
  searches: number;
  runs_forecast: number;
  ms: number;
}

interface PlanContext {
  from: PlanStation;
  to: PlanStation;
  date: string;
  time: string;
  model_stamp: string;
  data_until: string | null;
  stats: PlanStats;
}

export type PlanReason = "from_unserved" | "to_unserved" | "search_limit" | "no_connection";

export interface PlanResponse extends PlanContext {
  journeys: Journey[];
  reason: PlanReason | null;
}

export interface TimingsResponse extends PlanContext {
  route_key: string;
  window: { from: Moment; to: Moment };
  instances: Journey[];
}

export interface PlanParams {
  from: number;
  to: number;
  date: string;
  time: string;
}

export interface PlanTimingsParams extends PlanParams {
  route: string;
}

export function plan(from: number, to: number, date: string, time: string) {
  return apiFetch<PlanResponse>("/api/plan", { from, to, date, time });
}

export function planTimings(from: number, to: number, date: string, time: string, route: string) {
  return apiFetch<TimingsResponse>("/api/plan/timings", { from, to, date, time, route });
}
