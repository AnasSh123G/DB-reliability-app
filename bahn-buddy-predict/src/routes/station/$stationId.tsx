import { createFileRoute } from "@tanstack/react-router";
import { useAppChrome } from "@/components/AppShell";
import { BoardV2 } from "@/components/v2/BoardV2";
import { ShellV2 } from "@/components/v2/ShellV2";

export const Route = createFileRoute("/station/$stationId")({
  component: StationPageV2,
});

function StationPageV2() {
  const { stationId } = Route.useParams();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <ShellV2 locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <BoardV2 stationId={Number(stationId)} locale={locale} />
    </ShellV2>
  );
}
