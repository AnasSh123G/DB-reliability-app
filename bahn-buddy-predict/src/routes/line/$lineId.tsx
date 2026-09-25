import { createFileRoute } from "@tanstack/react-router";
import { useAppChrome } from "@/components/AppShell";
import { LineV2 } from "@/components/v2/LineV2";
import { ShellV2 } from "@/components/v2/ShellV2";

export const Route = createFileRoute("/line/$lineId")({
  component: LinePageV2,
});

function LinePageV2() {
  const { lineId } = Route.useParams();
  const { locale, setLocale, isDark, setIsDark } = useAppChrome();

  return (
    <ShellV2 locale={locale} isDark={isDark} setLocale={setLocale} setIsDark={setIsDark}>
      <LineV2 lineId={Number(lineId)} locale={locale} />
    </ShellV2>
  );
}
