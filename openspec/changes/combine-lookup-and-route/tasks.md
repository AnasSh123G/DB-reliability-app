# Tasks

## 1. Dated route forecast

- [x] 1.1 `train_route` in `src/dbrel/web/queries.py` takes `service_date` and forecasts the stops for it. The view parses `date` and answers 400 for anything but YYYY-MM-DD.
- [x] 1.2 `tests/test_corridor.py::test_train_route_forecasts_the_given_run_day` checks that the model input is built for the given day.
- [x] 1.3 `trainRoute`, `useTrainRoute`, `v2Paths.train` and `/train/<id>` carry the date.

## 2. List and route on one page

- [x] 2.1 `RunEntry` can be chosen (`onSelect`, `selected`) as well as followed (`link`).
- [x] 2.2 `TrainV2` shows its stops as `RunEntry` rows under `RunColumnHeader`. The user's stop is marked by the bar, the background and a teal name, with no label.
- [x] 2.3 `BoardV2` and the Prognose card choose a run (`chosen`, `onChoose`). The first entry at or after the chosen time is chosen when the result appears.
- [x] 2.4 `SearchV2` shows the list and the route in columns of 7 and 5 parts. The route takes the list's height (`ROUTE_HEIGHT`), and a board has a fixed one (`RESULT_HEIGHT`). Below 1024 px they stack, and choosing an entry scrolls to the route.
- [x] 2.5 The board and the route scroll inside their cards. The board starts at the first call at or after its moment, and the route at the user's stop.
- [x] 2.6 Choosing a stop opens its board in place of both cards, with the run marked. Back restores the list and the route.
- [x] 2.7 The route header drops the category and operator codes and shows "Line <name>".
- [x] 2.8 `ShellV2` widens to the larger of 64rem and 55vw.
- [x] 2.9 The route's stops and column header leave out the chevron (`chevron={false}`).
- [x] 2.10 `WINDOW_SIZE` and `FIRST_CHUNK` go to 6 runs. Earlier/Later move to a column left of the runs from 640 px up.

## 3. Checks

- [x] 3.1 Full backend suite passes. `tsc`, `eslint` (touched files), `prettier` and `npm run build` are clean.
- [x] 3.2 In the browser at 1076 px: the board and the route are 640 px tall. The board for Bochum Hbf at 11:00 starts at 11:01. RE10 82270 at Meerbusch-Osterath shows +5 min / 12:57 in both the list and the route.
- [x] 3.3 Choosing Krefeld-Oppum opens its board from 12:59 with RE10 82270 marked. Back restores the runs and the chosen route.
- [x] 3.5 At 1920 px the content is 1056 px wide. The list and the route are 565 px and 403 px wide, and the same height. Earlier/Later sit left of the runs. A stop's departure and platform line is not cut off.
- [x] 3.4 At 390 px the cards stack at 576 px tall each, with no sideways scroll.
