import { createFileRoute } from "@tanstack/react-router";
import { useAppChrome } from "@/components/AppShell";
import { TrainV2 } from "@/components/v2/TrainV2";
import { ShellV2 } from "@/components/v2/ShellV2";

export const Route = createFileRoute("/train/$trainId")({
  validateSearch: (search: Record<string, unknown>): { station?: number | undefined } => {
    const station = Number(search["station"]);
    return Number.isInteger(station) && station > 0 ? { station } : {};
  },
  component: TrainPageV2,
});

function TrainPageV2() {
  const { trainId } = Route.useParams();
  const { station } = Route.useSearch();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <ShellV2 locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <TrainV2 trainId={Number(trainId)} stationId={station} locale={locale} />
    </ShellV2>
  );
}
