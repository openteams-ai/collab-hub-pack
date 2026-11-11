import { describe, expect, it } from "vitest";

import type { Resource } from "./api";
import { loadGroupSizes } from "./groups";

describe("loadGroupSizes", () => {
  it("asks the directory once per group, however many models it gates", async () => {
    const asked: string[] = [];
    const get = async (path: string): Promise<Resource<{ members: unknown[] }>> => {
      asked.push(path);
      return { state: "ok", data: { members: path.includes("llm") ? [{}, {}] : [{}] } };
    };

    const sizes = await loadGroupSizes(["/llm", "/llm", "/vision"], get);

    expect(asked).toEqual(["api/model-access?group_path=%2Fllm", "api/model-access?group_path=%2Fvision"]);
    expect(sizes).toEqual(new Map([["/llm", 2], ["/vision", 1]]));
  });

  it("leaves out a group the directory did not answer for, so its size is not shown as zero", async () => {
    const get = async (): Promise<Resource<{ members: unknown[] }>> => ({ state: "error" });
    expect(await loadGroupSizes(["/llm"], get)).toEqual(new Map());
  });
});
