import { EmptyState as BaseEmptyState, Pill } from "@/design";
import type { IconName } from "@/design";

interface Props {
  icon: IconName;
  title: string;
  text: string;
  /** When the missing part arrives ("Llega en el paso 4"). Shown as a pill under the text. */
  when: string;
}

/** A designed empty state of a desk: what the section will hold and when it arrives. */
export function DeskEmptyState({ icon, title, text, when }: Props) {
  return <BaseEmptyState icon={icon} title={title} description={text} action={<Pill tone="acc">{when}</Pill>} />;
}
