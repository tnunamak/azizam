import { DiscoveryTabs } from "@/components/product/discovery-tabs";
import ModelsClient from "./models-client";

export default function Page() {
  return (
    <>
      <DiscoveryTabs current="models" />
      <p className="text-muted-foreground text-meta mb-4">
        Model names select an available route. A route pin keeps a conversation on one route;
        pins are accepted but not listed here.
      </p>
      <ModelsClient />
    </>
  );
}
