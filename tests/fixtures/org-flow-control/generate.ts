// Run with Bun and a local checkout of the real publisher. No service connections are made.
import { resolve } from "node:path";

const { createRedisTrafficControlPublisher } = await import(resolve(process.argv[2]!));
const configs = [
  ["disabled", { schema_version: 1, pause_until: null, rate_limit: null, concurrency_limit: null }],
  ["paused", { schema_version: 1, pause_until: "2030-01-01T00:00:01.234Z",
    rate_limit: { rps: 0.5, burst: 5 }, concurrency_limit: { max_in_flight: 2 } }],
  ["resumed", { schema_version: 1, pause_until: null,
    rate_limit: { rps: 0.5, burst: 5 }, concurrency_limit: { max_in_flight: 2 } }],
] as const;

for (const [index, [name, config]] of configs.entries()) {
  let serialized: string | undefined;
  const publisher = createRedisTrafficControlPublisher("redis://unused", {
    createClient: () => ({
      connect: async () => {},
      get: async () => null,
      send: async (command: string, args: string[]) => {
        if (command !== "EVAL") throw new Error("unexpected publisher command");
        serialized = args[5];
        return 1;
      },
      close: () => {},
    }),
  });
  await publisher.publish({ org_id: 42, version: index + 1, config });
  if (!serialized) throw new Error("publisher did not serialize the fixture");
  await Bun.write(new URL(`${name}.json`, import.meta.url), serialized + "\n");
}
