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

/** A Unix moment as the Berlin calendar and clock show it, including DST. */
export function berlinMoment(epoch: number): { date: string; time: string } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(epoch * 1000));
  const value = (kind: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === kind)?.value ?? "";
  return {
    date: `${value("year")}-${value("month")}-${value("day")}`,
    time: `${value("hour")}:${value("minute")}`,
  };
}

/** Journey duration in the page language. */
export function formatDuration(min: number, locale: Locale): string {
  const hours = Math.floor(min / 60);
  const minutes = min % 60;
  const unit = locale === "de" ? "Min." : "min";
  return hours > 0
    ? `${hours} ${locale === "de" ? "Std." : "h"} ${minutes} ${unit}`
    : `${minutes} ${unit}`;
}
