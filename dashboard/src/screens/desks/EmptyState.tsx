import { EmptyState as BaseEmptyState, Pill } from "@/design";
import type { IconName } from "@/design";

interface Props {
  icon: IconName;
  title: string;
  text: string;
  /** Optional note shown as a pill under the text. */
  when?: string;
}

/** A designed empty state of a desk: what the section will hold. */
export function DeskEmptyState({ icon, title, text, when }: Props) {
  return <BaseEmptyState icon={icon} title={title} description={text} action={when ? <Pill tone="acc">{when}</Pill> : undefined} />;
}
