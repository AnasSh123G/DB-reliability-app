// Every in-app link inside the v2 interface (the default, at /) is built here,
// so no v2 component can link into v1 (now under /v1) by accident. Spread
// the result into a <Link> or pass it to navigate().
export const v2Paths = {
  home: () => ({ to: "/" }) as const,
  train: (trainId: number) =>
    ({ to: "/train/$trainId", params: { trainId: String(trainId) } }) as const,
  station: (stationId: number) =>
    ({ to: "/station/$stationId", params: { stationId: String(stationId) } }) as const,
  line: (lineId: number) => ({ to: "/line/$lineId", params: { lineId: String(lineId) } }) as const,
};
