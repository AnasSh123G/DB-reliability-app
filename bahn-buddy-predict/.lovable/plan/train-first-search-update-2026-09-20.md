# Train-first search update

## What will change
- Make the train search the default, first tab while keeping journey search available as the second tab.
- Replace the single train text box with a compact search row for train/line, direction, stop, and time.
- Populate each field from the stored BahnPredict timetable data, narrowing later choices based on earlier selections.
- Show the matching service at the selected stop, including scheduled arrival/departure and their expected delayed times.
- Label nearby matching services as previous, selected, or next where the stored timetable supports them.
- Let users open the full stop-by-stop route from a result, while retaining the existing BahnPredict styling.

## Technical details
- Extend the local timetable helpers rather than introducing live data.
- Reuse the existing delay color rules and train overview.
- Add German and English labels for the new controls and result states.
- Keep the layout readable on mobile by stacking the controls and timing details.

## Verification
- Test train-first loading, dependent field choices, result selection, route expansion, language switching, and mobile/desktop layouts.
