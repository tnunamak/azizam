import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  description?: string;
  answer?: string;
  actions?: ReactNode;
}

export function PageHeader({
  title,
  description,
  answer,
  actions,
}: PageHeaderProps) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4 pb-8">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {(answer || description) && (
          <p className="text-muted-foreground mt-2 max-w-2xl text-sm">
            {answer || description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      )}
    </header>
  );
}
