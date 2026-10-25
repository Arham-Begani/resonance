// Small shared building blocks: buttons with loading state, status badges, callouts.
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { ApiFailure, failureMessage, failureTitle } from "../../api/client";
import { Icon, type IconName } from "./Icon";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md" | "lg";
  icon?: IconName;
  loading?: boolean;
  iconOnly?: boolean;
}

/**
 * While `loading`, the button stays focusable (aria-disabled + aria-busy) instead of becoming
 * `disabled`: browsers blur a focused element that becomes disabled, which would drop keyboard
 * and screen-reader users back to <body>. Clicks (and implicit form submission) are ignored.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon, loading = false, iconOnly = false, className, children, disabled, type, onClick, ...rest },
  ref,
) {
  const classes = [
    "btn",
    variant !== "secondary" ? `btn--${variant}` : "",
    size !== "md" ? `btn--${size}` : "",
    iconOnly ? "btn--icon" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={classes}
      disabled={disabled && !loading}
      aria-disabled={loading || undefined}
      aria-busy={loading || undefined}
      onClick={(event) => {
        if (loading) {
          event.preventDefault();
          return;
        }
        onClick?.(event);
      }}
      {...rest}
    >
      {loading ? <span className="spinner" aria-hidden="true" /> : icon ? <Icon name={icon} /> : null}
      {children}
    </button>
  );
});

/** "prediction" marks analytical (unmeasured) values: neutral text with a dashed edge. */
export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "prediction";

const TONE_ICON: Record<Tone, IconName | null> = {
  neutral: null,
  accent: null,
  prediction: null,
  success: "check",
  warning: "alert",
  danger: "x",
};

export function Badge({ tone = "neutral", icon, children }: { tone?: Tone; icon?: IconName | null; children: ReactNode }) {
  const name = icon === undefined ? TONE_ICON[tone] : icon;
  return (
    <span className={`badge${tone !== "neutral" ? ` badge--${tone}` : ""}`}>
      {name ? <Icon name={name} size={12} /> : null}
      {children}
    </span>
  );
}

export function PassFail({ passed, passLabel = "Pass", failLabel = "Fail" }: { passed: boolean; passLabel?: string; failLabel?: string }) {
  return passed ? <Badge tone="success">{passLabel}</Badge> : <Badge tone="danger">{failLabel}</Badge>;
}

export function Callout({
  tone = "neutral",
  icon,
  title,
  children,
  action,
}: {
  tone?: "neutral" | "accent" | "success" | "warning" | "danger";
  icon?: IconName;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
}) {
  const defaultIcon: IconName =
    tone === "danger" ? "alert" : tone === "warning" ? "alert" : tone === "success" ? "check" : "info";
  return (
    <div className={`callout${tone !== "neutral" ? ` callout--${tone}` : ""}`}>
      <Icon name={icon ?? defaultIcon} />
      <div className="stack-1 callout__body">
        {title ? <strong>{title}</strong> : null}
        {children ? <div>{children}</div> : null}
        {action ? <div className="row row--wrap callout__actions">{action}</div> : null}
      </div>
    </div>
  );
}

/** A failed operation, explained calmly, with an optional retry. */
export function ErrorCallout({ error, onRetry, retryLabel = "Try again", extra }: { error: unknown; onRetry?: () => void; retryLabel?: string; extra?: ReactNode }) {
  // A cancellation is the user's own choice and a not-yet-built endpoint is not a failure:
  // present both calmly.
  const cancelled = error instanceof ApiFailure && (error.code === "cancelled" || error.code === "not_implemented");
  return (
    <Callout
      tone={cancelled ? "neutral" : "danger"}
      icon={cancelled ? "info" : undefined}
      title={failureTitle(error)}
      action={onRetry ? <Button size="sm" icon="refresh" onClick={onRetry}>{retryLabel}</Button> : undefined}
    >
      {failureMessage(error)}
      {extra}
    </Callout>
  );
}
