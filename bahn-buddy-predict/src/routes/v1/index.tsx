import { createFileRoute } from "@tanstack/react-router";
import { AppShell, useAppChrome } from "@/components/AppShell";
import { TrainSearch } from "@/components/TrainSearch";

export const Route = createFileRoute("/v1/")({
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
        content: "Historical delay averages for German trains — expected delay, timings, and train info.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Index,
});

function Index() {
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <AppShell locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <TrainSearch locale={locale} />
    </AppShell>
  );
}
