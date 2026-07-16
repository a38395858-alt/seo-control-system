export type Review = {
  is_seo_content_fit: boolean;
  same_topic_as_seed: boolean;
  search_intent: string;
  recommended_action: string;
  reason: string;
  confidence: number;
};

export type ExpansionResult = {
  keywords: string[];
  requests_made: number;
  stop_reason: string;
  debug_logs: Array<{ level: string; event: string; code: string; message: string }>;
};

export type LibraryKeyword = {
  id: number;
  keyword: string;
  category: string | null;
  search_intent: string | null;
  demand_estimate: number | null;
  search_volume: number | null;
  is_seo_content_fit: number | null;
};

export type Score = { keyword: string; keyword_difficulty: number; difficulty_level: string; opportunity_score: number };
