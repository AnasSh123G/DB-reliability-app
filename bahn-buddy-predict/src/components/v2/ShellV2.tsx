// The v2 shell: BahnPredict's own departure-board identity (see
// openspec/changes/redesign-v2-visual-identity/design.md), scoped by the `v2` class below onto
// its own tokens in styles.css. Behaviour, wording and data stay exactly as v1's -- locale and
// theme come from v1's useAppChrome, so both interfaces share one stored choice.
import { Moon, Sun } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { copy, type Locale } from "@/lib/i18n";
import { icons } from "./icons";
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
  const path = useRouterState({ select: (state) => state.location.pathname });
  const planning = path === "/plan";
  const tabs = [
    {
      key: "lookup",
      link: v2Paths.home(),
      label: t.lookupTab,
      Icon: icons.search,
      active: !planning,
    },
    { key: "plan", link: v2Paths.plan(), label: t.planner, Icon: icons.planner, active: planning },
  ];

  return (
    <div
      className={`v2 ${isDark ? "dark" : ""} min-h-screen bg-ink text-paper font-sans antialiased`}
    >
      <div className="mx-auto max-w-[max(64rem,55vw)] px-5 py-6 sm:px-8 sm:py-9">
        <header className="flex items-center justify-between gap-1 border-b sm:gap-3 border-line pb-4">
          <Link {...v2Paths.home()} className="flex min-w-0 items-center gap-1.5 sm:gap-2.5">
            <span className="inline-grid size-8 place-items-center rounded-md bg-teal text-action-foreground">
              <icons.train className="size-5" aria-hidden="true" />
            </span>
            <div className="leading-none">
              <p className="font-display font-semibold text-[15px] sm:text-[17.67px] tracking-tight">
                BahnPredict
              </p>
              <p className="text-[11px] sm:text-[13.67px] text-fog mt-0.5">{t.subtitle}</p>
            </div>
          </Link>
          <div className="flex shrink-0 items-center gap-1 sm:gap-2">
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
                className="size-7 sm:size-8 rounded text-fog hover:bg-ink hover:text-paper aria-pressed:bg-teal aria-pressed:text-action-foreground"
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
                className="size-7 sm:size-8 rounded text-fog hover:bg-ink hover:text-paper aria-pressed:bg-teal aria-pressed:text-action-foreground"
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
                    ? "rounded px-2 sm:px-3 bg-teal font-mono text-action-foreground hover:bg-teal hover:text-action-foreground"
                    : "rounded px-2 sm:px-3 font-mono text-fog hover:bg-ink hover:text-paper"
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
                    ? "rounded px-2 sm:px-3 bg-teal font-mono text-action-foreground hover:bg-teal hover:text-action-foreground"
                    : "rounded px-2 sm:px-3 font-mono text-fog hover:bg-ink hover:text-paper"
                }
              >
                EN
              </Button>
            </div>
          </div>
        </header>

        {/* Two tabs: looking up a train, line or station (with their pages), and the planner. */}
        <nav
          aria-label={locale === "de" ? "Bereiche" : "Sections"}
          className="mt-5 grid grid-cols-2 rounded-md border border-line bg-panel p-1 sm:inline-grid sm:w-auto"
        >
          {tabs.map(({ key, link, label, Icon, active }) => (
            <Link
              key={key}
              {...link}
              aria-current={active ? "page" : undefined}
              className={`inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded px-2 py-2 font-display text-[13px] font-semibold sm:gap-2 sm:px-4 sm:text-sm ${
                active ? "bg-teal text-action-foreground" : "text-fog hover:bg-ink hover:text-paper"
              }`}
            >
              <Icon className="size-4 shrink-0" aria-hidden="true" />
              {label}
            </Link>
          ))}
        </nav>

        <section className="mt-5 sm:mt-6">{children}</section>
      </div>
    </div>
  );
}
