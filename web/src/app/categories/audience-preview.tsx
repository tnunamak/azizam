import type { ReactNode } from "react";
import { Callout } from "@/components/product/status-ui";

/** One item that one app, acting for one person, can now use (gains) or can no longer use (losses), and the category that moves it. */
export type AudienceEntry = {
  item_id: string;
  category_id: string; category_label: string;
  app_id: string; app_name: string | null;
  person_id: string; person_label: string | null;
  person_plan_id: string; app_plan_id: string;
};
/** An app and person whose plans the gateway could not read, so it did not compare them. */
export type AudienceCannotTell = {
  app_id: string; app_name: string | null;
  person_id: string; person_label: string | null;
  person_plan_id: string | null; app_plan_id: string | null;
  reason: string;
};
export type Audience = { gains: AudienceEntry[]; losses: AudienceEntry[]; cannot_tell: AudienceCannotTell[] };
/** The audience fields on a candidate or restore preview. `audience` is null when the gateway cannot tell, and `audience_reason` then says why. */
export type AudiencePreview = {
  audience: Audience | null;
  audience_reason: "admin_store_not_configured" | "admin_store_unreadable" | null;
};

/** Whether the owner must look before a change is turned on. An unknown audience counts: it is never read as "no change". */
export function audienceNeedsReview(audience: Audience | null | undefined): boolean {
  return !audience || audience.gains.length > 0 || audience.losses.length > 0 || audience.cannot_tell.length > 0;
}

const who = (appName: string | null, personLabel: string | null) =>
  (appName || "Unnamed app") + " for " + (personLabel || "no person");

/** One line: the app and person, then the item and the category that moves it. */
const line = (entry: AudienceEntry) =>
  who(entry.app_name, entry.person_label) + ": " + entry.item_id + " (" + (entry.category_label || entry.category_id) + ")";

/** One entry is one item for one app and person. A category can hold several items, so the category alone is not unique. */
const entryKey = (entry: AudienceEntry) => [entry.item_id, entry.category_id, entry.app_id, entry.person_id].join("|");

function Group({ title, children }: { title: string; children: ReactNode }) {
  return <div className="space-y-2">
    <p className="text-body font-medium">{title}</p>
    <ul className="space-y-2 text-meta">{children}</ul>
  </div>;
}

/** Who a category change reaches, item by item. Shared by the category list, the rules editor and the restore preview. */
export function AudienceSummary({ audience }: { audience: Audience | null | undefined }) {
  return <section aria-label="Who this change reaches" className="space-y-3">
    <h3 className="text-body font-medium">Who this change reaches</h3>
    <p className="text-meta">Apps and people that exist now, listed by item and category. Apps and people added later are not listed.</p>
    {!audience && <Callout as="div" severity="attention" headingLevel={3} title="We couldn't check who this change reaches." />}
    {audience && !audienceNeedsReview(audience) && <p className="text-meta">No app or person gains or loses a category.</p>}
    {audience && audience.gains.length > 0 && <Group title="Can now use">
      {audience.gains.map((entry) => <li key={entryKey(entry)}>{line(entry)}</li>)}
    </Group>}
    {audience && audience.losses.length > 0 && <Group title="Can no longer use">
      {audience.losses.map((entry) => <li key={entryKey(entry)}>{line(entry)}</li>)}
    </Group>}
    {audience && audience.cannot_tell.length > 0 && <Group title="Can't tell">
      {audience.cannot_tell.map((entry) => <li key={entry.app_id + "|" + entry.person_id}>{who(entry.app_name, entry.person_label) + ": " + entry.reason}</li>)}
    </Group>}
  </section>;
}
