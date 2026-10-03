import Link from "next/link";
import { PageHeader } from "@/components/product/page-header";
import { Button } from "@/components/ui/button";
export default function Gallery() {
  return (
    <>
      <PageHeader
        title="Design system"
        answer="The component gallery has moved."
      />
      <Button asChild>
        <Link href="/design">Open the design system</Link>
      </Button>
    </>
  );
}
