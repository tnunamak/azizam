import { DiscoveryTabs } from "@/components/product/discovery-tabs";
import ModelsClient from "./models-client";

export default function Page() {
  return (
    <>
      <DiscoveryTabs current="models" />
      <ModelsClient />
    </>
  );
}
