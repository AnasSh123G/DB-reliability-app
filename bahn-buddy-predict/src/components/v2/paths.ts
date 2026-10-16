// Every in-app link inside the v2 interface (the default, at /) is built here,
// so no v2 component can link into v1 (now under /v1) by accident. Spread
// the result into a <Link> or pass it to navigate().
export const v2Paths = {
  plan: (search?: { from?: number; to?: number; date?: string; time?: string; option?: number }) =>
    ({ to: "/plan", search: search ?? {} }) as const,
  home: () => ({ to: "/" }) as const,
  // `stationId`: the stop the user came from, so the page shows a route that calls there.
  // `date`: the run's day (YYYY-MM-DD), so the stops are forecast for that day, not today.
  train: (trainId: number, stationId?: number | undefined, date?: string | undefined) =>
    ({
      to: "/train/$trainId",
      params: { trainId: String(trainId) },
      search: { station: stationId, date },
    }) as const,
  // The board of a station is shown on the search page, under the form. `moment`: a date
  // (YYYY-MM-DD) and time (HH:MM) to show it for, instead of now.
  station: (stationId: number, moment?: { date: string; time: string }) =>
    ({
      to: "/",
      search: moment
        ? { station: stationId, date: moment.date, time: moment.time }
        : { station: stationId },
    }) as const,
  line: (lineId: number) => ({ to: "/line/$lineId", params: { lineId: String(lineId) } }) as const,
};
