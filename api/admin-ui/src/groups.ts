import { type Resource, getJson } from "./api";

/** Where the panel reads the people in one directory group. */
export function groupMembersPath(groupPath: string): string {
  return `api/model-access?group_path=${encodeURIComponent(groupPath)}`;
}

/**
 * How many people are in each of *groupPaths*, asking once per distinct group:
 * several models gated by one group share one directory call. A group whose
 * answer did not come back is absent, never counted as empty.
 */
export async function loadGroupSizes(
  groupPaths: string[],
  get: (path: string) => Promise<Resource<{ members: unknown[] }>> = getJson,
): Promise<Map<string, number>> {
  const distinct = [...new Set(groupPaths)];
  const answers = await Promise.all(distinct.map((groupPath) => get(groupMembersPath(groupPath))));
  const sizes = new Map<string, number>();
  distinct.forEach((groupPath, index) => {
    const answer = answers[index];
    if (answer.state === "ok") sizes.set(groupPath, answer.data.members.length);
  });
  return sizes;
}
