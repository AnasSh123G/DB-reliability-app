// The stations this browser plans journeys from and to most, counted on each search. Kept in
// localStorage, so they stay on this device only; a blocked or cleared storage just means no list.
const KEY = "dbrel.planner.stations";
const KEEP = 30;

export interface FrequentStation {
  id: number;
  name: string;
  count: number;
}

export function frequentStations(): FrequentStation[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (entry): entry is FrequentStation =>
          typeof entry?.id === "number" &&
          typeof entry?.name === "string" &&
          entry.name !== "" &&
          typeof entry?.count === "number",
      )
      .sort((a, b) => b.count - a.count);
  } catch {
    return [];
  }
}

/** Count one more search from or to each of these stations. */
export function recordStations(stations: { id: number; name: string }[]) {
  const counts = new Map(frequentStations().map((entry) => [entry.id, entry]));
  for (const station of stations) {
    if (!station.name) continue;
    const entry = counts.get(station.id);
    counts.set(station.id, {
      id: station.id,
      name: station.name,
      count: (entry?.count ?? 0) + 1,
    });
  }
  const kept = [...counts.values()].sort((a, b) => b.count - a.count).slice(0, KEEP);
  try {
    localStorage.setItem(KEY, JSON.stringify(kept));
  } catch {
    // Storage is blocked: the list simply stays empty.
  }
}

/**
 * Suggestions for what was typed, led by the most used stations whose name contains it (at most
 * `lead` of them), then the remaining suggestions in their own order.
 */
export function frequentFirst<T extends { id: number; name: string }>(
  query: string,
  results: T[],
  frequent: FrequentStation[],
  lead = 3,
): { id: number; name: string }[] {
  const needle = query.trim().toLocaleLowerCase("de");
  const first = frequent
    .filter((entry) => entry.name.toLocaleLowerCase("de").includes(needle))
    .slice(0, lead)
    .map(({ id, name }) => ({ id, name }));
  const ids = new Set(first.map((entry) => entry.id));
  return [...first, ...results.filter((result) => !ids.has(result.id))];
}
