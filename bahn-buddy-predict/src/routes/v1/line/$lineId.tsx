import { createFileRoute } from "@tanstack/react-router";
import { AppShell, useAppChrome } from "@/components/AppShell";
import { LineDetail } from "@/components/LineDetail";

export const Route = createFileRoute("/v1/line/$lineId")({
  component: LinePage,
});

function LinePage() {
  const { lineId } = Route.useParams();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <AppShell locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <LineDetail lineId={Number(lineId)} locale={locale} />
    </AppShell>
  );
}
