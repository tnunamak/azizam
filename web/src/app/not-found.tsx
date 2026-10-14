import Link from "next/link";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { Button } from "@/components/ui/button";
export default function NotFound() {
  return (
    <>
      <PageHeader
        title="Page not found"
        answer="Go Home to find what you need."
      />
      <EmptyState
        title="Find your way home"
        description="We couldn't find a page at this address. It may have moved or been removed. Check the address for a typo, or go Home to find what you need."
        action={
          <Button asChild>
            <Link prefetch={false} href="/">Home</Link>
          </Button>
        }
      />
    </>
  );
}
