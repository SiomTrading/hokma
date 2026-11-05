/** Shapes of /api/learn/*. */

export interface TopicSummary {
  slug: string;
  title: string;
}

export interface Topic extends TopicSummary {
  body: string;
}

export interface SearchHit extends TopicSummary {
  snippet: string;
  score: number;
}

export type FirstDayStep = "first_conversation" | "first_project" | "first_image";

export interface FirstDayProgress {
  ready: boolean;
  done: number;
  total: number;
  steps: Record<FirstDayStep, boolean>;
}

export const FIRST_DAY_STEPS: readonly FirstDayStep[] = ["first_conversation", "first_project", "first_image"];
