import { createFileRoute } from "@tanstack/react-router";
import { useAppChrome } from "@/components/AppShell";
import { TrainV2 } from "@/components/v2/TrainV2";
import { ShellV2 } from "@/components/v2/ShellV2";

export const Route = createFileRoute("/train/$trainId")({
  component: TrainPageV2,
});

function TrainPageV2() {
  const { trainId } = Route.useParams();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <ShellV2 locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <TrainV2 trainId={Number(trainId)} locale={locale} className="mx-auto w-full max-w-3xl" />
    </ShellV2>
  );
}
