import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { getDepartmentSpacesApi, getJoinedSpacesApi, getMineSpacesApi } from "~/api/knowledge";
import { useGetOrgToolList } from "~/hooks/queries/data-provider";

type Named = { id?: string | number; name?: string };

/**
 * Names for knowledge base ids nobody has named yet (they returned no chunk and
 * are not in the user's current selection), taken from the lists the chat
 * knowledge picker already uses:
 *  - document knowledge bases from the organisation list;
 *  - knowledge spaces from mine + joined + department, merged.
 * Both kinds share one id space. The lists only hold what the user may use, so
 * an id they cannot see (no access, deleted) stays unnamed and its chip is
 * skipped. Space ids are deliberately NOT fetched one by one: the info endpoint
 * answers 403 for a space the user cannot open, and the request layer turns a
 * 403 into a full page redirect.
 *
 * `enabled` is false on the share view; nothing is requested while every id is
 * already named.
 */
export function useKnowledgeBaseNames(ids: string[], enabled: boolean): Record<string, string> {
    const active = enabled && ids.length > 0;

    const { data: orgList } = useGetOrgToolList(
        { page: 1, page_size: 200, action: "use" },
        { enabled: active },
    );
    const orgNames = useMemo(() => toNameMap(orgList as Named[] | undefined), [orgList]);

    // Spaces are only needed for ids the organisation list does not name; wait
    // for the list so a document knowledge base never triggers the space calls.
    const needSpaces = active && !!orgList && ids.some((id) => !orgNames[id]);
    const { data: spaceNames } = useQuery({
        queryKey: ["KnowledgeBaseNames", "spaces"],
        queryFn: async () => {
            const [mine, joined, department] = await Promise.all([
                getMineSpacesApi(),
                getJoinedSpacesApi(),
                getDepartmentSpacesApi(),
            ]);
            return toNameMap([...mine, ...joined, ...department]);
        },
        enabled: needSpaces,
        staleTime: 5 * 60 * 1000,
        retry: false,
        refetchOnWindowFocus: false,
    });

    return useMemo(() => {
        const names: Record<string, string> = {};
        for (const id of ids) {
            const name = orgNames[id] || spaceNames?.[id];
            if (name) names[id] = name;
        }
        return names;
        // eslint-disable-next-line react-hooks/exhaustive-deps -- ids.join stands for ids
    }, [ids.join(","), orgNames, spaceNames]);
}

function toNameMap(items: Named[] | undefined): Record<string, string> {
    const map: Record<string, string> = {};
    for (const item of items ?? []) {
        if (item?.id !== undefined && item?.name) map[String(item.id)] = item.name;
    }
    return map;
}
