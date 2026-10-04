import { useQuery } from "@tanstack/react-query";
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
