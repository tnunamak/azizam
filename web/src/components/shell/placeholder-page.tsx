"use client";

import Link from "next/link";
import { ArrowUpRight, type LucideIcon } from "lucide-react";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { Button } from "@/components/ui/button";

export function PlaceholderPage({
  title,
  answer,
  description,
  icon,
}: {
  title: string;
  answer: string;
  description: string;
  icon?: LucideIcon;
}) {
  return (
    <>
      <PageHeader title={title} answer={answer} />
      <div className="placeholder-content">
        <EmptyState
          title="Coming in this release"
          description={description}
          icon={icon}
          action={
            <Button variant="outline" asChild>
              <Link href="/apps">
                Connect an app
                <ArrowUpRight />
              </Link>
            </Button>
          }
        />
      </div>
    </>
  );
}
