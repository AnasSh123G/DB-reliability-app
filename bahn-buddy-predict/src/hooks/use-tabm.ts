import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ModelInput } from "@/lib/api";
import { predictBatch, type ModelPrediction } from "@/lib/tabm";

interface Keyed {
  key: string | number;
  model_input: ModelInput | null;
}

// Runs the current model, once, over every item that has a model_input --
// batched in one inference pass per view, not one per row. Returns a map from
// each item's key to its prediction; empty until inference finishes, so a
// view falls back to "average only" exactly as it would with no model at all.
export function useModelPredictions(
  stamp: string | null | undefined,
  items: Keyed[],
): Map<string | number, ModelPrediction> {
  const withInput = items.filter(
    (item): item is Keyed & { model_input: ModelInput } => item.model_input !== null,
  );
  // Keyed by the encoded inputs themselves, not just the rows' ids: the same
  // trains on another date (weekday, holiday) are different inputs, and keying
  // by id alone handed back the first date's cached predictions.
  const inputs = withInput.map((item) => [item.key, item.model_input]);

  const { data } = useQuery({
    queryKey: ["model-predictions", stamp, inputs],
    queryFn: async () => {
      const predictions = await predictBatch(
        stamp as string,
        withInput.map((item) => item.model_input),
      );
      return new Map(withInput.map((item, i) => [item.key, predictions[i]]));
    },
    enabled: Boolean(stamp) && withInput.length > 0,
    staleTime: Infinity, // a call's encoded input means the same thing forever
  });

  return data ?? new Map();
}

/**
 * The model's forecasts for several groups of items, one inference batch per group.
 *
 * Groups run one after another, in order: the first group is forecast alone, so what the page
 * shows first is ready first, and a later group starts only when the one before it has finished
 * (the model runs one batch at a time anyway). A group's batch is cached by its encoded inputs,
 * so adding a group never forecasts the earlier ones again.
 *
 * @param stamp - the model's stamp, null or undefined while unknown.
 * @param groups - items per group, the group to forecast first first.
 * @returns every forecast made so far, by item key, and whether the first group is done.
 */
export function useModelPredictionGroups(
  stamp: string | null | undefined,
  groups: Keyed[][],
): { predictions: Map<string | number, ModelPrediction>; firstDone: boolean } {
  const queryClient = useQueryClient();
  const prepared = groups.map((items) => {
    const withInput = items.filter(
      (item): item is Keyed & { model_input: ModelInput } => item.model_input !== null,
    );
    const queryKey = [
      "model-predictions",
      stamp,
      withInput.map((item) => [item.key, item.model_input]),
    ];
    return { withInput, queryKey };
  });
  const results = useQueries({
    queries: prepared.map(({ withInput, queryKey }, index) => ({
      queryKey,
      queryFn: async () => {
        const predictions = await predictBatch(
          stamp as string,
          withInput.map((item) => item.model_input),
        );
        return new Map(withInput.map((item, i) => [item.key, predictions[i]] as const));
      },
      // Each group waits for the ones before it to finish (or fail). The page re-renders when a
      // group finishes, which is what lets the next one start.
      enabled:
        Boolean(stamp) &&
        withInput.length > 0 &&
        prepared.slice(0, index).every((earlier) => {
          if (earlier.withInput.length === 0) return true;
          const status = queryClient.getQueryState(earlier.queryKey)?.status;
          return status === "success" || status === "error";
        }),
      staleTime: Infinity,
    })),
  });
  const predictions = new Map<string | number, ModelPrediction>();
  for (const result of results) {
    for (const [key, value] of result.data ?? []) {
      if (value !== undefined) predictions.set(key, value);
    }
  }
  const first = results[0];
  // Done also when there is nothing to wait for: no model at all, or no item with an input.
  const firstDone =
    groups.length > 0 &&
    (!stamp ||
      prepared[0]?.withInput.length === 0 ||
      first?.data !== undefined ||
      first?.isError === true);
  return { predictions, firstDone };
}
