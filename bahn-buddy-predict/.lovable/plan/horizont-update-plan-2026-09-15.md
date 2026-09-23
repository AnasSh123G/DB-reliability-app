# Horizont update plan

## What will change
- Add a second search mode for a specific ICE/IC train name or number.
- Give each supported train a concise stop-by-stop timetable, with the selected route emphasized.
- Make the train name in station-search results open that same timetable view.
- Replace gradients, blur, and glass effects with restrained solid-color panels.
- Remove the risk badge and delay-distribution graph; keep the predicted delay time prominent.

## Technical details
- Extend the local train data model with reusable train services and stops.
- Reuse one timetable component in both search flows.
- Keep the existing station/date/time forecast behavior and German interface copy.

## Verification
- Test station search, direct train search, opening the train timetable, and desktop/mobile layouts.
