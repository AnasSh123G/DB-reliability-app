import { Moon, Sun } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { copy, type Locale } from "@/lib/i18n";

const LOCALE_KEY = "bahnpredict:locale";
const THEME_KEY = "bahnpredict:dark";

function readLocale(): Locale {
  try {
    return localStorage.getItem(LOCALE_KEY) === "en" ? "en" : "de";
  } catch {
    return "de";
  }
}

function readDark(): boolean {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return stored === null ? true : stored === "1";
  } catch {
    return true;
  }
}

// Persisted in localStorage (not component state) so the choice survives
// navigating between routes -- each route mounts this shell independently,
// there is no shared layout-route state to lift it into.
export function useAppChrome() {
  const [locale, setLocaleState] = useState<Locale>(readLocale);
  const [isDark, setIsDarkState] = useState<boolean>(readDark);

  const setLocale = (next: Locale) => {
    setLocaleState(next);
    try {
      localStorage.setItem(LOCALE_KEY, next);
    } catch {
      // per-viewer convenience only; fine if storage is unavailable
    }
  };

  const setIsDark = (next: boolean) => {
    setIsDarkState(next);
    try {
      localStorage.setItem(THEME_KEY, next ? "1" : "0");
    } catch {
      // per-viewer convenience only; fine if storage is unavailable
    }
  };

  return { locale, setLocale, isDark, setIsDark, t: copy[locale] };
}

interface Props {
  locale: Locale;
  isDark: boolean;
  setLocale: (locale: Locale) => void;
  setIsDark: (dark: boolean) => void;
  children: ReactNode;
}

export function AppShell({ locale, isDark, setLocale, setIsDark, children }: Props) {
  const t = copy[locale];

  return (
    <div className={`${isDark ? "dark" : ""} min-h-screen bg-ink text-paper font-sans antialiased`}>
      <div className="mx-auto max-w-5xl px-5 py-6 sm:px-8 sm:py-9">
        <div className="mb-4 flex justify-end gap-2">
          <div
            className="inline-flex rounded-xl border border-line bg-panel p-1"
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
              className="size-8 rounded-lg text-fog hover:bg-ink hover:text-paper aria-pressed:bg-teal aria-pressed:text-action-foreground"
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
              className="size-8 rounded-lg text-fog hover:bg-ink hover:text-paper aria-pressed:bg-teal aria-pressed:text-action-foreground"
            >
              <Moon className="size-4" />
            </Button>
          </div>
          <div className="inline-flex rounded-xl border border-line bg-panel p-1" role="group" aria-label="Language">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setLocale("de")}
              aria-pressed={locale === "de"}
              className={
                locale === "de"
                  ? "rounded-lg bg-teal text-action-foreground hover:bg-teal hover:text-action-foreground"
                  : "rounded-lg text-fog hover:bg-ink hover:text-paper"
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
                  ? "rounded-lg bg-teal text-action-foreground hover:bg-teal hover:text-action-foreground"
                  : "rounded-lg text-fog hover:bg-ink hover:text-paper"
              }
            >
              EN
            </Button>
          </div>
        </div>
        <header className="flex items-center justify-between">
          <Link to="/v1" className="flex items-center gap-2.5">
            <span className="inline-grid size-8 place-items-center rounded-xl bg-teal font-display font-semibold text-action-foreground">
              H
            </span>
            <div className="leading-none">
              <p className="font-display font-semibold text-[15px] tracking-tight">BahnPredict</p>
              <p className="text-[11px] text-fog mt-0.5">{t.subtitle}</p>
            </div>
          </Link>
          <div className="flex items-center gap-2 text-[11px] text-fog">
            <span className="size-1.5 rounded-full bg-teal" />
            <span>{t.live}</span>
          </div>
        </header>

        <section className="mt-6 sm:mt-8">{children}</section>
      </div>
    </div>
  );
}
