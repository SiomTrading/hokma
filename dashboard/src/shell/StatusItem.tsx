import { Dot } from "@/design";

interface StatusItemProps {
  label: string;
  value: string;
  /** Pulsing green dot before the label (engine online). */
  live?: boolean;
}

/** One entry of the status bar: `<label> value`. */
export function StatusItem({ label, value, live }: StatusItemProps) {
  return (
    <span>
      {live ? <Dot live color="var(--ok)" style={{ display: "inline-block", marginRight: 6 }} /> : null}
      <b>{label}</b> {value}
    </span>
  );
}
