import type { ReactNode } from "react";

import type { Resource } from "./api";
import { initials } from "./avatar";

/** A page's title, what it is for, and its own control on the right if it has one. */
export function PageHeader({ title, children, end }: { title: string; children: ReactNode; end?: ReactNode }) {
  return (
    <div className="page-header">
      <div className="page-header-text">
        <h2>{title}</h2>
        <p>{children}</p>
      </div>
      {end}
    </div>
  );
}

/** A row of mutually exclusive filters. `aria-pressed` says which is on. */
export function Tabs<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="tabs" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** Someone, as their initials beside their address, with an optional second line. */
export function Person({ email, sub }: { email: string; sub?: string | null }) {
  return (
    <span className="named">
      <span className="avatar small" aria-hidden="true">
        {initials({ email })}
      </span>
      {sub ? (
        <span className="stack">
          <span>{email}</span>
          <span className="sub">{sub}</span>
        </span>
      ) : (
        <span>{email}</span>
      )}
    </span>
  );
}

/** Every failure this panel can show, in words an operator can act on. */
export function Failure({ resource, subject }: { resource: Resource<unknown>; subject: string }) {
  if (resource.state === "forbidden") {
    return <p className="bad">This account is not allowed to see {subject}.</p>;
  }
  if (resource.state === "refused") {
    return <p className="bad">That request was declined ({resource.reason}).</p>;
  }
  if (resource.state === "unavailable") {
    return (
      <p className="bad">
        This hub cannot show {subject}: it is not configured for it ({resource.reason}).
      </p>
    );
  }
  if (resource.state === "upstream-error") {
    return (
      <p className="bad">
        {subject} could not be read because another service did not answer ({resource.reason}).
      </p>
    );
  }
  return <p className="bad">Could not load {subject}.</p>;
}

export function Loading({ subject }: { subject: string }) {
  return <p className="empty">Loading {subject}…</p>;
}
