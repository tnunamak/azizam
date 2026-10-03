import type { ReactNode } from "react";
import { Inbox, type LucideIcon } from "lucide-react";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";

export interface EmptyStateProps {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: LucideIcon;
}
export function EmptyState({
  title,
  description,
  action,
  icon: Icon = Inbox,
}: EmptyStateProps) {
  return (
    <Empty>
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Icon aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        {description && <EmptyDescription>{description}</EmptyDescription>}
      </EmptyHeader>
      {action && <EmptyContent>{action}</EmptyContent>}
    </Empty>
  );
}
