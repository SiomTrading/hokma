import { useId } from "react";
import type {
  AnchorHTMLAttributes,
  ButtonHTMLAttributes,
  CSSProperties,
  HTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { cx } from "./cx";
import { Icon } from "./icons";
import type { IconName } from "./icons";

/* ---------- Surfaces ---------- */

interface PaneProps extends HTMLAttributes<HTMLElement> {
  as?: "div" | "section" | "aside";
}

export function Pane({ as: Tag = "div", className, ...rest }: PaneProps) {
  return <Tag className={cx("pane", className)} {...rest} />;
}

export function Row({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cx("row", className)} {...rest} />;
}

/* ---------- Buttons ---------- */

type ButtonVariant = "default" | "primary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "md" | "sm";
}

const VARIANT_CLASS: Record<ButtonVariant, string | false> = {
  default: false,
  primary: "pri",
  ghost: "ghost",
  danger: "bad",
};

export function Button({ variant = "default", size = "md", className, type = "button", ...rest }: ButtonProps) {
  return <button type={type} className={cx("btn", VARIANT_CLASS[variant], size === "sm" && "sm", className)} {...rest} />;
}

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: IconName;
  /** Accessible name (required: the button has no text). */
  label: string;
  on?: boolean;
  size?: number;
}

export function IconButton({ icon, label, on, size = 17, className, type = "button", ...rest }: IconButtonProps) {
  return (
    <button type={type} className={cx("ib", on && "on", className)} aria-label={label} aria-pressed={on === undefined ? undefined : on} {...rest}>
      <Icon name={icon} size={size} />
    </button>
  );
}

/* ---------- Small indicators ---------- */

type PillTone = "default" | "ok" | "warn" | "acc" | "org" | "bad";

interface PillProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: PillTone;
}

export function Pill({ tone = "default", className, ...rest }: PillProps) {
  return <span className={cx("pill", tone !== "default" && tone, className)} {...rest} />;
}

interface DotProps extends HTMLAttributes<HTMLSpanElement> {
  live?: boolean;
  /** Any CSS color, usually a token such as `var(--ok)`. */
  color?: string;
}

export function Dot({ live, color, className, style, ...rest }: DotProps) {
  return <span className={cx("dot", live && "live", className)} style={{ ...(color ? { color } : null), ...style }} aria-hidden="true" {...rest} />;
}

interface MeterProps {
  /** 0 to 100. */
  value: number;
  tone?: "acc" | "warn" | "bad";
  large?: boolean;
  label: string;
}

export function Meter({ value, tone = "acc", large, label }: MeterProps) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className={cx("meter", large && "lg", tone !== "acc" && tone)} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

interface ToggleProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onChange" | "children"> {
  checked: boolean;
  onChange?: (next: boolean) => void;
  /** Accessible name (required). */
  label: string;
}

export function Toggle({ checked, onChange, label, className, type = "button", ...rest }: ToggleProps) {
  return (
    <button
      type={type}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={cx("tg", checked && "on", className)}
      onClick={() => onChange?.(!checked)}
      {...rest}
    />
  );
}

/* ---------- Navigation ---------- */

interface NavItemProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: IconName;
  label: string;
  active?: boolean;
  /** Shortcut text shown on the right (hidden when the menu is collapsed). */
  kbd?: string;
  iconSize?: number;
  /** A small dot after the label, with its accessible text (for example "new version available"). */
  badge?: string;
}

export function NavItem({ icon, label, active, kbd, iconSize = 17, badge, className, type = "button", ...rest }: NavItemProps) {
  return (
    <button type={type} className={cx("nav", active && "on", className)} aria-label={badge ? label + ", " + badge : label} aria-current={active ? "page" : undefined} {...rest}>
      <Icon name={icon} size={iconSize} />
      <span className="hide-c">{label}</span>
      {badge ? <span className="nav-badge" aria-hidden="true" title={badge} /> : null}
      {kbd ? <span className="kbd hide-c">{kbd}</span> : null}
    </button>
  );
}

interface NavLinkProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "children"> {
  icon: IconName;
  label: string;
  active?: boolean;
  kbd?: string;
  iconSize?: number;
}

/**
 * A `NavItem` that is a real link: a plain click can be handled by the page (`onClick` with `preventDefault`), and
 * the middle button, Ctrl, Cmd or Shift click are left to the browser, which opens the address in another tab.
 */
export function NavLink({ icon, label, active, kbd, iconSize = 17, className, ...rest }: NavLinkProps) {
  return (
    <a className={cx("nav", active && "on", className)} aria-label={label} aria-current={active ? "page" : undefined} {...rest}>
      <Icon name={icon} size={iconSize} />
      <span className="hide-c">{label}</span>
      {kbd ? <span className="kbd hide-c">{kbd}</span> : null}
    </a>
  );
}

interface TabProps {
  label: string;
  active?: boolean;
  /** CSS color for the dot at the left. */
  dotColor?: string;
  onSelect: () => void;
  onClose?: () => void;
  closeLabel: string;
}

export function Tab({ label, active, dotColor, onSelect, onClose, closeLabel }: TabProps) {
  return (
    <div className={cx("tab", active && "on")}>
      <button type="button" className="tl" aria-current={active ? "page" : undefined} onClick={onSelect}>
        <Dot color={dotColor} />
        {label}
      </button>
      {onClose ? (
        <button type="button" className="tx" aria-label={closeLabel} onClick={onClose}>
          ×
        </button>
      ) : null}
    </div>
  );
}

interface SegmentedProps<V extends string> {
  options: ReadonlyArray<{ value: V; label: string }>;
  value: V;
  onChange: (value: V) => void;
  label: string;
}

export function Segmented<V extends string>({ options, value, onChange, label }: SegmentedProps<V>) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" className={o.value === value ? "on" : undefined} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

interface OptionProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onSelect"> {
  selected?: boolean;
  onSelect?: () => void;
}

/** Selectable card (radio behavior). Put several inside `role="radiogroup"`. */
export function Option({ selected, onSelect, className, children, type = "button", ...rest }: OptionProps) {
  return (
    <button type={type} role="radio" aria-checked={!!selected} className={cx("opt", selected && "on", className)} onClick={onSelect} {...rest}>
      {children}
    </button>
  );
}

interface StepsProps {
  labels: readonly string[];
  /** Zero-based index of the current step. */
  current: number;
}

export function Steps({ labels, current }: StepsProps) {
  return (
    <ol style={{ display: "flex", flexWrap: "wrap", gap: 14, margin: 0, padding: 0, listStyle: "none" }}>
      {labels.map((label, i) => (
        <li key={label} className={cx("step", i === current && "on", i < current && "done")} aria-current={i === current ? "step" : undefined}>
          <i aria-hidden="true">{i < current ? "✓" : String(i + 1)}</i>
          <span>{label}</span>
        </li>
      ))}
    </ol>
  );
}

/* ---------- Content ---------- */

interface BubbleProps extends HTMLAttributes<HTMLDivElement> {
  from: "user" | "assistant";
}

export function Bubble({ from, className, ...rest }: BubbleProps) {
  return <div className={cx("bub", from === "user" ? "u" : "a", className)} {...rest} />;
}

export function Chip({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cx("chip", className)} {...rest} />;
}

interface GlyphProps extends HTMLAttributes<HTMLDivElement> {
  initial: string;
  /** CSS color of the letter and the ring. Background is a tint of it. */
  color?: string;
  size?: number;
}

export function Glyph({ initial, color = "var(--acc)", size = 44, style, className, ...rest }: GlyphProps) {
  const sized: CSSProperties =
    size === 44 ? {} : { width: size, height: size, borderRadius: Math.round(size * 0.3), fontSize: Math.round(size * 0.34) };
  return (
    <div className={cx("glyph", className)} style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)`, ...sized, ...style }} {...rest}>
      {initial}
    </div>
  );
}

/* ---------- Forms ---------- */

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx("in", className)} {...rest} />;
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx("in", className)} {...rest} />;
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className="sel">
      <select className={cx("in", className)} {...rest}>
        {children}
      </select>
      <Icon name="chevronDown" size={14} strokeWidth={2} />
    </span>
  );
}

interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  children: (control: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => ReactNode;
}

/** Label + control + hint/error, wired for accessibility. */
export function Field({ label, hint, error, children }: FieldProps) {
  const id = useId();
  const noteId = hint || error ? `${id}-note` : undefined;
  return (
    <div className="fld">
      <label htmlFor={id} className="lbl">
        {label}
      </label>
      {children({ id, "aria-describedby": noteId, ...(error ? { "aria-invalid": true as const } : {}) })}
      {error ? (
        <span id={noteId} className="err" role="alert">
          {error}
        </span>
      ) : hint ? (
        <span id={noteId} className="hint">
          {hint}
        </span>
      ) : null}
    </div>
  );
}
