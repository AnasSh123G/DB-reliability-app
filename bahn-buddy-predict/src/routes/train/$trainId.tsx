import { createFileRoute } from "@tanstack/react-router";
import { AppShell, useAppChrome } from "@/components/AppShell";
import { TrainOverview } from "@/components/TrainOverview";

export const Route = createFileRoute("/train/$trainId")({
  component: TrainPage,
});

function TrainPage() {
  const { trainId } = Route.useParams();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <AppShell locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <TrainOverview trainId={Number(trainId)} locale={locale} className="mx-auto w-full max-w-3xl" />
    </AppShell>
  );
}
