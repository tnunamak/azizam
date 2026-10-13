import Link from "next/link";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { Button } from "@/components/ui/button";
export default function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" answer="This page may have moved." />
      <EmptyState
        title="Find your way home"
        description="Use the navigation to explore your gateway."
        action={
          <Button asChild>
            <Link prefetch={false} href="/">Go home</Link>
          </Button>
        }
      />
    </>
  );
}
