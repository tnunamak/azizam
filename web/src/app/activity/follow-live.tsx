"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/product/relative-time";

export function FollowLive({
  following,
  onChange,
  updatedAt,
}: {
  following: boolean;
  onChange: (next: boolean) => void;
  updatedAt: number | null;
}) {
  return (
    <div className="flex items-center gap-2">
      <Button type="button" variant="outline" aria-pressed={following} onClick={() => onChange(!following)}>
        {following ? "Pause" : "Follow live"}
      </Button>
      {typeof updatedAt === "number" && (
        <span className="text-meta text-muted-foreground">
          Updated <RelativeTime ts={updatedAt} />
        </span>
      )}
    </div>
  );
}

export function useFollowLive() {
  return useState(false);
}
