import { createFileRoute } from "@tanstack/react-router";
import { useAppChrome } from "@/components/AppShell";
import { SearchV2 } from "@/components/v2/SearchV2";
import { ShellV2 } from "@/components/v2/ShellV2";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "BahnPredict — German Train Delay Averages" },
      {
        name: "description",
        content:
          "Search a station, line or train and see historical delay averages plus every stop of the route.",
      },
      { property: "og:title", content: "BahnPredict — German Train Delay Averages" },
      {
        property: "og:description",
        content:
          "Historical delay averages for German trains — expected delay, timings, and train info.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  // A station (and optionally a date and time) to show the board of under the form.
  validateSearch: (
    search: Record<string, unknown>,
  ): { station?: number | undefined; date?: string | undefined; time?: string | undefined } => {
    const station = Number(search["station"]);
    if (!Number.isInteger(station) || station <= 0) return {};
    const date = String(search["date"] ?? "");
    const time = String(search["time"] ?? "");
    return /^\d{4}-\d{2}-\d{2}$/.test(date) && /^\d{2}:\d{2}$/.test(time)
      ? { station, date, time }
      : { station };
  },
  component: IndexV2,
});

function IndexV2() {
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();
  const { station, date, time } = Route.useSearch();

  return (
    <ShellV2 locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <SearchV2
        locale={locale}
        initialStation={station === undefined ? undefined : { id: station, date, time }}
      />
    </ShellV2>
  );
}
