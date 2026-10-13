import { createFileRoute, redirect } from "@tanstack/react-router";

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const TIME = /^\d{2}:\d{2}$/;

// A station's board is shown on the search page, under the form. This address, which links and
// bookmarks still use, leads there.
export const Route = createFileRoute("/station/$stationId")({
  beforeLoad: ({ params, location }) => {
    const query = new URLSearchParams(location.searchStr);
    const date = query.get("date") ?? "";
    const time = query.get("time") ?? "";
    const station = Number(params.stationId);
    throw redirect({
      to: "/",
      search: DATE.test(date) && TIME.test(time) ? { station, date, time } : { station },
    });
  },
});
