import type { Locale } from "@/lib/i18n";

/** A scheduled time as the clock shows it: a run after midnight is 00:17, not 24:17. */
export const clockTime = (time: string | null) =>
  time === null ? null : time.replace(/^\d+/, (h) => String(Number(h) % 24).padStart(2, "0"));

/**
 * A date as the locale writes it: "20.10.2026" or "20/10/2026".
 *
 * @param day - YYYY-MM-DD
 * @param locale - the page language
 * @returns the formatted date
 */
export function formatDay(day: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale === "de" ? "de-DE" : "en-GB", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${day}T00:00:00Z`));
}

/**
 * A date moved by some days.
 *
 * @param day - YYYY-MM-DD
 * @param days - how many days to move it, negative for earlier
 * @returns the new date, YYYY-MM-DD
 */
export function addDays(day: string, days: number): string {
  const moved = new Date(`${day}T00:00:00Z`);
  moved.setUTCDate(moved.getUTCDate() + days);
  return moved.toISOString().slice(0, 10);
}

/**
 * A run's day, short: "Mo., 12.10." or "Mon 12/10".
 *
 * @param day - YYYY-MM-DD
 * @param locale - the page language
 * @returns the weekday and date
 */
export function formatRunDay(day: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale === "de" ? "de-DE" : "en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    timeZone: "UTC",
  }).format(new Date(`${day}T00:00:00Z`));
}
