export type JevMemoryStats = {
  judged: number;
  saved: number;
  failed: number;
  costMicro: number;
};

export type JevMemoryFeed = {
  count: number;
  strip: Array<"saved" | "skipped" | "fail">;
  last: Array<{ text: string; p: number; status: "saved" | "skipped" }>;
};

declare module "claude-code" {
  interface PluginState {
    "jev-memory": {
      stats: JevMemoryStats;
      pending: string[];
      judging: boolean;
      feed: JevMemoryFeed;
    };
  }
}
