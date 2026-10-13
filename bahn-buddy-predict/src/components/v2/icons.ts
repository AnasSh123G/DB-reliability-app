// One icon for each kind of field or value, so the same kind looks the same on every v2 view
// (search, Prognose card, station board, train page). Icons always sit beside text.
import {
  ArrowRightFromLine,
  ArrowRightLeft,
  ArrowRightToLine,
  CalendarClock,
  CalendarDays,
  CirclePlay,
  Clock3,
  Flag,
  Hourglass,
  Map,
  MapPin,
  Search,
  TrainFront,
  type LucideIcon,
} from "lucide-react";

export const icons = {
  state: Map,
  search: Search,
  // Two arrows running opposite ways: a line's two directions.
  direction: ArrowRightLeft,
  stop: MapPin,
  date: CalendarDays,
  time: Clock3,
  // Coming in to the stop, and leaving it.
  arrival: ArrowRightToLine,
  departure: ArrowRightFromLine,
  // The timetable's time, the time lost, and the time the train is expected to come in.
  scheduled: CalendarClock,
  delay: Hourglass,
  expected: ArrowRightToLine,
  // The start of a journey, and its finish flag.
  startsHere: CirclePlay,
  endsHere: Flag,
  train: TrainFront,
} as const satisfies Record<string, LucideIcon>;

export type IconKind = keyof typeof icons;
