import { createFileRoute } from "@tanstack/react-router";
import { AppShell, useAppChrome } from "@/components/AppShell";
import { StationBoard } from "@/components/StationBoard";

export const Route = createFileRoute("/v1/station/$stationId")({
  component: StationPage,
});

function StationPage() {
  const { stationId } = Route.useParams();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <AppShell locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <StationBoard stationId={Number(stationId)} locale={locale} />
    </AppShell>
  );
}
