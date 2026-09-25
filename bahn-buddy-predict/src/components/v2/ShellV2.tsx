// The v2 shell: BahnPredict's own departure-board identity (see
// openspec/changes/redesign-v2-visual-identity/design.md), scoped by the `v2` class below onto
// its own tokens in styles.css. Behaviour, wording and data stay exactly as v1's -- locale and
// theme come from v1's useAppChrome, so both interfaces share one stored choice.
import { Moon, Sun } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { copy, type Locale } from "@/lib/i18n";
import { v2Paths } from "./paths";

interface Props {
  locale: Locale;
  isDark: boolean;
  setLocale: (locale: Locale) => void;
  setIsDark: (dark: boolean) => void;
  children: ReactNode;
}

export function ShellV2({ locale, isDark, setLocale, setIsDark, children }: Props) {
  const t = copy[locale];

  return (
    <div
      className={`v2 ${isDark ? "dark" : ""} min-h-screen bg-ink text-paper font-sans antialiased`}
    >
      <div className="mx-auto max-w-5xl px-5 py-6 sm:px-8 sm:py-9">
        <div className="mb-4 flex justify-end gap-2">
          <div
            className="inline-flex rounded-md border border-line bg-panel p-1"
            role="group"
            aria-label={locale === "de" ? "Farbschema" : "Color theme"}
          >
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setIsDark(false)}
              aria-label={locale === "de" ? "Heller Modus" : "Light mode"}
              aria-pressed={!isDark}
              title={locale === "de" ? "Heller Modus" : "Light mode"}
              className="size-8 rounded text-fog hover:bg-ink hover:text-paper aria-pressed:bg-teal aria-pressed:text-action-foreground"
            >
              <Sun className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setIsDark(true)}
              aria-label={locale === "de" ? "Dunkler Modus" : "Dark mode"}
              aria-pressed={isDark}
              title={locale === "de" ? "Dunkler Modus" : "Dark mode"}
              className="size-8 rounded text-fog hover:bg-ink hover:text-paper aria-pressed:bg-teal aria-pressed:text-action-foreground"
            >
              <Moon className="size-4" />
            </Button>
          </div>
          <div
            className="inline-flex rounded-md border border-line bg-panel p-1"
            role="group"
            aria-label="Language"
          >
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setLocale("de")}
              aria-pressed={locale === "de"}
              className={
                locale === "de"
                  ? "rounded bg-teal font-mono text-action-foreground hover:bg-teal hover:text-action-foreground"
                  : "rounded font-mono text-fog hover:bg-ink hover:text-paper"
              }
            >
              DE
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setLocale("en")}
              aria-pressed={locale === "en"}
              className={
                locale === "en"
                  ? "rounded bg-teal font-mono text-action-foreground hover:bg-teal hover:text-action-foreground"
                  : "rounded font-mono text-fog hover:bg-ink hover:text-paper"
              }
            >
              EN
            </Button>
          </div>
        </div>
        <header className="flex items-center justify-between border-b border-line pb-4">
          <Link {...v2Paths.home()} className="flex items-center gap-2.5">
            <span className="inline-grid size-8 place-items-center rounded bg-teal font-mono font-semibold text-action-foreground">
              H
            </span>
            <div className="leading-none">
              <p className="font-display font-semibold text-[15px] tracking-tight">BahnPredict</p>
              <p className="text-[11px] text-fog mt-0.5">{t.subtitle}</p>
            </div>
          </Link>
          <div className="flex items-center gap-2 text-[11px] font-mono uppercase tracking-wide text-fog">
            <span className="size-1.5 rounded-full bg-teal shadow-[0_0_6px_var(--teal)]" />
            <span>{t.live}</span>
          </div>
        </header>

        <section className="mt-6 sm:mt-8">{children}</section>
      </div>
    </div>
  );
}
