import { describe, expect, mock, test } from "claude-code/testing";

// Stubs the world beneath the mod: an in-memory memory file, a Treg whose
// answers the test sets, and a record of toasts.
function world(on: any, initial = "") {
  const w = {
    file: initial,
    pending: [] as string[],
    toasts: [] as string[],
    tregCalls: [] as string[][],
    treg: (_argv: string[]): { exitCode: number; stdout: string; stderr: string } => ({ exitCode: 1, stdout: "", stderr: "down" }),
  };
  const clock = mock.clock(on);
  mock.env(on, { HOME: "/home/me" });
  // A test can't submit as the person (`$.prompt.submit` carries a plugin
  // origin), so the mod's list of typed prompts is answered from here instead.
  const isPending = (e: any) => e.plugin === "jev-memory" && e.key === "pending";
  on("state.get", ($: any, e: any, next: any) => (isPending(e) ? { value: { value: [...w.pending] } } : next(e)));
  on("state.set", ($: any, e: any, next: any) => {
    if (!isPending(e)) return next(e);
    w.pending = [...e.value];
    return { value: undefined };
  });
  on("session.start", ($: any, e: any) => ({ cwd: e.cwd }));
  on("command.register", ($: any, e: any) => ({ value: { command: e.name } }));
  on("turn.complete", () => ({ text: "" }));
  on("prompt.submit", ($: any, e: any) => ({ text: e.text }));
  on("prompt.context", ($: any, e: any) => ({ blocks: e.blocks }));
  on("fs.read", () => {
    if (!w.file) throw new Error("ENOENT");
    return { value: w.file };
  });
  on("fs.write", ($: any, e: any) => {
    w.file = e.text;
    return { value: undefined };
  });
  on("ui.toast", ($: any, e: any) => {
    w.toasts.push(e.text);
    return { value: undefined };
  });
  on("process.run", ($: any, e: any) => {
    w.tregCalls.push([...e.argv]);
    return { value: w.treg([...e.argv]) };
  });
  return { w, clock };
}

const jev = (answers: Record<string, number>, charged = 20) => () => ({
  exitCode: 0,
  stderr: "",
  stdout: JSON.stringify({
    result: { answers: Object.fromEntries(Object.entries(answers).map(([k, v]) => [k, { noul: v }])) },
    _treg: { http_status: 200, call_id: "c1", charged_micro: charged },
  }),
});

// Beneath every plugin, the engine draws nothing in this band (once per test).
const stubbed = new WeakSet<object>();
const engineDrawsNothing = (on: any) => {
  if (stubbed.has(on)) return;
  stubbed.add(on);
  on("ui.render", () => ({ type: "Box", children: [] }));
};

async function start($: any, on: any) {
  engineDrawsNothing(on);
  await $.session.start({ surface: "terminal", isInteractive: true, cwd: "/work" } as any);
  return $.ui.mount({
    plugin: "jev-memory",
    surface: "terminal",
    component: "AbovePrompt",
    props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 120 },
  } as any);
}

async function finishTurn($: any, w: any, clock: any, prompts: string[]) {
  w.pending.push(...prompts);
  await $.turn.complete({ reason: "answer", answer: "ok", durationMs: 1 } as any);
  await clock.advance(1); // runs the judging the turn handed off
}

describe("jev-memory", () => {
  test("saves sentences at 0.8 or higher, toasts, and counts the turn", async ($, on) => {
    const { w, clock } = world(on);
    w.treg = jev({ q1: 0.93, q2: 0.1, q3: 0.79 });
    const ui = await start($, on);
    expect(await ui.find({ type: "Text", text: "◆ jev memory" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "0 remembered" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "0 turns judged" })).toBeDefined();

    await finishTurn($, w, clock, ["Always use pnpm here. Fix the login bug please. Maybe prefer tabs."]);

    expect(w.file).toBe("- Always use pnpm here.\n");
    expect(w.toasts).toEqual(["jev saved: Always use pnpm here. (p=0.93)"]);
    expect(await ui.find({ type: "Text", text: "1 remembered" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "1 turns judged" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "■ 1 saved" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "░ 2 skipped" })).toBeDefined();
    // each judged sentence shows Jev's probability
    expect(await ui.find({ type: "Text", text: "0.93" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "0.10" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "Always use pnpm here." })).toBeDefined();

    // one request, JSON output, the sentences escaped into the state
    expect(w.tregCalls.length).toBe(1);
    const argv = w.tregCalls[0];
    expect(argv[0]).toBe("/home/me/.local/bin/treg");
    expect(argv).toContain("--json");
    const body = JSON.parse(argv[argv.indexOf("--data") + 1]);
    expect(body.model).toBe("typesafe/jev-1.13");
    expect(Object.keys(body.questions)).toEqual(["q1", "q2", "q3"]);
    await ui.unmount();
  });

  test("plays the coin clip with the toast, and stays silent when nothing is saved", async ($, on) => {
    const { w, clock } = world(on);
    const played: any[] = [];
    on("audio.play", ($: any, e: any) => {
      played.push(e.clip);
      return { value: undefined };
    });
    w.treg = jev({ q1: 0.93 });
    const ui = await start($, on);
    await finishTurn($, w, clock, ["Always use pnpm here."]);
    expect(played).toEqual([{ asset: "sounds/coin.wav" }]);
    w.treg = jev({ q1: 0.1 });
    await finishTurn($, w, clock, ["Fix the login bug please."]);
    expect(played.length).toBe(1);
    await ui.unmount();
  });

  test("shows the Treg cost in dollars", async ($, on) => {
    const { w, clock } = world(on);
    w.treg = jev({ q1: 0.1 }, 300);
    const ui = await start($, on);
    await finishTurn($, w, clock, ["Please rename the helper function."]);
    expect(await ui.find({ type: "Text", text: "$0.00030" })).toBeDefined();
    await ui.unmount();
  });

  test("a failed Treg call is skipped silently and counted", async ($, on) => {
    const { w, clock } = world(on, "- Existing rule here.\n");
    const ui = await start($, on);
    await finishTurn($, w, clock, ["Always use pnpm here."]);

    expect(w.file).toBe("- Existing rule here.\n");
    expect(w.toasts).toEqual([]);
    expect(await ui.find({ type: "Text", text: "1 failed" })).toBeDefined();
    await ui.unmount();
  });

  test("garbage from Treg counts as a failure", async ($, on) => {
    const { w, clock } = world(on);
    w.treg = () => ({ exitCode: 0, stdout: "not json", stderr: "" });
    const ui = await start($, on);
    await finishTurn($, w, clock, ["Always use pnpm here."]);
    expect(w.file).toBe("");
    expect(await ui.find({ type: "Text", text: /1 failed/ })).toBeDefined();
    await ui.unmount();
  });

  test("no duplicates, and lines are trimmed to 300 characters", async ($, on) => {
    const { w, clock } = world(on, "- always use pnpm here.\n");
    const long = `Never ${"x".repeat(400)}.`;
    w.treg = jev({ q1: 0.9, q2: 0.95 });
    const ui = await start($, on);
    await finishTurn($, w, clock, [`Always use pnpm here. ${long}`]);

    const lines = w.file.trim().split("\n");
    expect(lines.length).toBe(2);
    expect(lines[0]).toBe("- always use pnpm here.");
    expect(lines[1].length).toBe(2 + 300);
    expect(w.toasts.length).toBe(1);
    await ui.unmount();
  });

  test("sends at most six sentences in one request", async ($, on) => {
    const { w, clock } = world(on);
    w.treg = jev({});
    const ui = await start($, on);
    await finishTurn($, w, clock, [Array.from({ length: 9 }, (_, i) => `Sentence number ${i} here.`).join(" ")]);
    const argv = w.tregCalls[0];
    const body = JSON.parse(argv[argv.indexOf("--data") + 1]);
    expect(Object.keys(body.questions).length).toBe(6);
    expect(w.tregCalls.length).toBe(1);
    await ui.unmount();
  });

  test("the turn does not wait for Treg", async ($, on) => {
    const { w, clock } = world(on);
    let released = false;
    w.treg = () => {
      released = true;
      return jev({ q1: 0.9 })();
    };
    const ui = await start($, on);
    w.pending.push(...["Always use pnpm here."]);
    await $.turn.complete({ reason: "answer", answer: "ok", durationMs: 1 } as any);
    expect(released).toBe(false);
    expect(w.file).toBe("");
    await clock.advance(1);
    expect(released).toBe(true);
    expect(w.file).toBe("- Always use pnpm here.\n");
    await ui.unmount();
  });

  test("prompts that are not the user's own are not judged", async ($, on) => {
    const { w, clock } = world(on);
    w.treg = jev({ q1: 0.99 });
    const ui = await start($, on);
    await $.prompt.submit({ text: "Always use pnpm here." } as any); // origin: a plugin
    await $.turn.complete({ reason: "answer", answer: "ok", durationMs: 1 } as any);
    await clock.advance(1);
    expect(w.tregCalls).toEqual([]);
    await ui.unmount();
  });

  test("subagent turns and unanswered turns judge nothing", async ($, on) => {
    const { w, clock } = world(on);
    w.treg = jev({ q1: 0.99 });
    const ui = await start($, on);
    w.pending.push(...["Always use pnpm here."]);
    await $.turn.complete({ reason: "answer", answer: "ok", durationMs: 1, agentId: "sub" } as any);
    await $.turn.complete({ reason: "aborted", answer: "", durationMs: 1, isAborted: true } as any);
    await clock.advance(1);
    expect(w.tregCalls).toEqual([]);
    await ui.unmount();
  });

  test("saved lines enter the first message of the next session", async ($, on) => {
    world(on, "- Always use pnpm here.\n- Never mock the database.\n");
    await $.session.start({ surface: "terminal", isInteractive: true, cwd: "/work" } as any);
    const { blocks } = await $.prompt.context({ blocks: [{ name: "currentDate", text: "today" }] } as any);
    const block = blocks.find((b: any) => b.name === "Preferences the user stated in earlier sessions");
    expect(block.text).toBe("- Always use pnpm here.\n- Never mock the database.");
    expect(blocks[0].name).toBe("currentDate");
  });

  test("with nothing saved, the context only says jev-memory does the saving", async ($, on) => {
    world(on);
    await $.session.start({ surface: "terminal", isInteractive: true, cwd: "/work" } as any);
    const { blocks } = await $.prompt.context({ blocks: [{ name: "currentDate", text: "today" }] } as any);
    expect(blocks.map((b: any) => b.name)).toEqual(["currentDate", "How preferences are remembered here"]);
    expect(blocks[1].text).toContain("Don't save preferences yourself");
  });

  test("Claude Code's own memory section is dropped", async ($, on) => {
    world(on);
    on("prompt.section", () => ({ text: "built-in memory instructions" }));
    await $.session.start({ surface: "terminal", isInteractive: true, cwd: "/work" } as any);
    const { text } = await $.prompt.section({ name: "memory" } as any);
    expect(text).toBeNull();
  });

  test("/jev-memory lists, and forget <n> removes line n", async ($, on) => {
    const { w } = world(on, "- One rule.\n- Two rule.\n- Three rule.\n");
    await $.session.start({ surface: "terminal", isInteractive: true, cwd: "/work" } as any);
    const run = (args: string) => $.command.run({ command: "jev-memory", args, origin: { kind: "composer" } } as any);

    expect((await run("")).text).toBe("1. One rule.\n2. Two rule.\n3. Three rule.");
    expect((await run("forget 2")).text).toContain("Forgot 2: Two rule.");
    expect(w.file).toBe("- One rule.\n- Three rule.\n");
    expect((await run("forget 9")).text).toContain("No saved line 9");
    expect(w.file).toBe("- One rule.\n- Three rule.\n");
    expect((await run("bogus")).text).toContain("Usage");
  });

  test("the line says so when nothing is saved", async ($, on) => {
    world(on);
    await $.session.start({ surface: "terminal", isInteractive: true, cwd: "/work" } as any);
    const { text } = await $.command.run({ command: "jev-memory", args: "", origin: { kind: "composer" } } as any);
    expect(text).toContain("Nothing saved yet");
  });
});
