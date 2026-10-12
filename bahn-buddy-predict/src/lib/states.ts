import type { Locale } from "@/lib/i18n";

// The Bundesländer a search can be narrowed to: the codes the backend's station.state holds
// (dbrel.web.queries.STATES), each with its name in both languages.
export const STATES = [
  { code: "DE-BW", de: "Baden-Württemberg", en: "Baden-Württemberg" },
  { code: "DE-BY", de: "Bayern", en: "Bavaria" },
  { code: "DE-BE", de: "Berlin", en: "Berlin" },
  { code: "DE-BB", de: "Brandenburg", en: "Brandenburg" },
  { code: "DE-HB", de: "Bremen", en: "Bremen" },
  { code: "DE-HH", de: "Hamburg", en: "Hamburg" },
  { code: "DE-HE", de: "Hessen", en: "Hesse" },
  { code: "DE-MV", de: "Mecklenburg-Vorpommern", en: "Mecklenburg-Western Pomerania" },
  { code: "DE-NI", de: "Niedersachsen", en: "Lower Saxony" },
  { code: "DE-NW", de: "Nordrhein-Westfalen", en: "North Rhine-Westphalia" },
  { code: "DE-RP", de: "Rheinland-Pfalz", en: "Rhineland-Palatinate" },
  { code: "DE-SL", de: "Saarland", en: "Saarland" },
  { code: "DE-SN", de: "Sachsen", en: "Saxony" },
  { code: "DE-ST", de: "Sachsen-Anhalt", en: "Saxony-Anhalt" },
  { code: "DE-SH", de: "Schleswig-Holstein", en: "Schleswig-Holstein" },
  { code: "DE-TH", de: "Thüringen", en: "Thuringia" },
] as const;

export type StateCode = (typeof STATES)[number]["code"];

/**
 * The states in alphabetical order of their name in `locale`.
 *
 * @param locale - the page's language.
 * @returns code and display name of each state.
 */
export function statesFor(locale: Locale): { code: StateCode; name: string }[] {
  return STATES.map((s) => ({ code: s.code, name: s[locale] })).sort((a, b) =>
    a.name.localeCompare(b.name, locale),
  );
}

/**
 * Whether `value` is one of the state codes (e.g. a value read back from browser storage).
 *
 * @param value - any string.
 * @returns true for a known code.
 */
export function isStateCode(value: string): value is StateCode {
  return STATES.some((s) => s.code === value);
}
